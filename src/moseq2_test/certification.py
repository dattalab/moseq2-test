"""Reproducible baseline certification across the three implemented suites."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moseq2_test import WORKER_PROTOCOL_VERSION, __version__
from moseq2_test.config import resource
from moseq2_test.data import CacheLayout
from moseq2_test.errors import ExitCode, InvalidConfiguration, Moseq2TestError, UnexpectedResult
from moseq2_test.models import RunRecord
from moseq2_test.provenance import controller_environment, sha256_file
from moseq2_test.registry import fixture_manifest, known_failures, profile, source_lock, wheel_lock
from moseq2_test.reporting import load_run

CERTIFICATION_VERSION = 1
SUITE_MATRIX = (
    ("install-smoke", None),
    ("historical-regression", "historical-v1"),
    ("pipeline-smoke", "pipeline-smoke-v1"),
)
ACCEPTED_COMMAND_CLASSES = {"passed", "known-failure", "allowed-pass"}


@dataclass(frozen=True)
class RequirementResult:
    """One independently reviewable baseline assertion."""

    id: str
    description: str
    status: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class SuiteEvidence:
    profile: str
    exit_code: int
    run_directory: str
    run_id: str | None
    status: str
    duration_seconds: float


class ResourceSampler:
    """Best-effort, dependency-free process/disk measurements for CI sizing."""

    def __init__(
        self, workspace: Path, output: Path, cache: Path, *, expected_objects: int
    ) -> None:
        self.workspace = workspace
        self.output = output
        self.cache = cache
        self.expected_objects = expected_objects
        self.started_at = time.monotonic()
        self.cache_bytes_before = _tree_size(cache)
        object_root = CacheLayout(cache).objects
        self.object_bytes_before = _tree_size(object_root)
        self.cache_objects_before = _file_count(object_root)
        self.peak_rss_bytes = 0
        self.peak_workspace_bytes = 0
        self.peak_output_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=5)
        self._measure()
        cache_bytes_after = _tree_size(self.cache)
        object_root = CacheLayout(self.cache).objects
        object_bytes_after = _tree_size(object_root)
        cache_objects_after = _file_count(object_root)
        return {
            "measurement_version": 1,
            "wall_seconds": time.monotonic() - self.started_at,
            "peak_controller_tree_rss_bytes": self.peak_rss_bytes,
            "peak_workspace_bytes": self.peak_workspace_bytes,
            "peak_output_bytes": self.peak_output_bytes,
            "cache_bytes_before": self.cache_bytes_before,
            "cache_bytes_after": cache_bytes_after,
            "cache_objects_before": self.cache_objects_before,
            "cache_objects_after": cache_objects_after,
            "object_bytes_before": self.object_bytes_before,
            "object_bytes_after": object_bytes_after,
            "downloaded_object_bytes": max(0, object_bytes_after - self.object_bytes_before),
            "derived_cache_bytes_added": max(
                0,
                (cache_bytes_after - object_bytes_after)
                - (self.cache_bytes_before - self.object_bytes_before),
            ),
            "expected_object_count": self.expected_objects,
            "cache_mode": (
                "warm"
                if self.cache_objects_before >= self.expected_objects
                else "cold-or-partial"
            ),
            "sampling_interval_seconds": 0.5,
            "rss_scope": "controller process and observable Linux descendants",
        }

    def _sample(self) -> None:
        while not self._stop.wait(0.5):
            self._measure()

    def _measure(self) -> None:
        self.peak_rss_bytes = max(self.peak_rss_bytes, _process_tree_rss(os.getpid()))
        self.peak_workspace_bytes = max(self.peak_workspace_bytes, _tree_size(self.workspace))
        self.peak_output_bytes = max(self.peak_output_bytes, _tree_size(self.output))


def _tree_size(root: Path) -> int:
    if not root.exists():
        return 0
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0
    total = 0
    for directory, _subdirectories, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            try:
                total += (Path(directory) / filename).stat().st_size
            except OSError:
                continue
    return total


def _file_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def _linux_children(pid: int) -> list[int]:
    children_file = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return [int(value) for value in children_file.read_text().split()]
    except (OSError, ValueError):
        return []


def _rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _process_tree_rss(pid: int) -> int:
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += _rss(current)
        pending.extend(_linux_children(current))
    return total


def _require(
    identifier: str, description: str, condition: bool, **evidence: Any
) -> RequirementResult:
    return RequirementResult(
        id=identifier,
        description=description,
        status="passed" if condition else "failed",
        evidence=evidence,
    )


def _counts(values: list[str]) -> dict[str, int]:
    return {value: values.count(value) for value in sorted(set(values))}


def _required_files(run_directory: Path) -> dict[str, bool]:
    names = (
        "run.json",
        "summary.md",
        "junit.xml",
        "resolved-config.yml",
        "candidates.json",
        "environment.json",
    )
    return {name: (run_directory / name).is_file() for name in names}


def _common_requirements(
    record: RunRecord,
    run_directory: Path,
    *,
    expected_profile: str,
    baseline_lock: str,
    expected_fixture_sets: list[str],
    suite_exit_code: int,
) -> list[RequirementResult]:
    sources = source_lock(baseline_lock)
    wheels = wheel_lock("moseq2-baseline-linux-py37-v1")
    expected_commits = {item.name: item.commit for item in sources.sources}
    candidate_commits = {item.package: item.source_commit for item in record.candidates}
    expected_hashes = {item.package: item.sha256 for item in wheels.wheels}
    candidate_hashes = {item.package: item.sha256 for item in record.candidates}
    files = _required_files(run_directory)
    return [
        _require(
            f"{expected_profile}.accepted",
            "suite exits successfully and its canonical record is accepted",
            suite_exit_code == int(ExitCode.ACCEPTED)
            and record.status == "accepted"
            and record.failure_stage is None,
            exit_code=suite_exit_code,
            status=record.status,
            failure_stage=record.failure_stage,
        ),
        _require(
            f"{expected_profile}.identity",
            "suite, source lock, wheel lock, fixtures, schema, and worker protocol are exact",
            record.profile == expected_profile
            and record.source_lock == baseline_lock
            and record.wheel_lock == wheels.lock_id
            and record.fixture_sets == expected_fixture_sets
            and record.schema_version == 1
            and record.worker_protocol_version == WORKER_PROTOCOL_VERSION,
            profile=record.profile,
            source_lock=record.source_lock,
            wheel_lock=record.wheel_lock,
            fixture_sets=record.fixture_sets,
            schema_version=record.schema_version,
            worker_protocol_version=record.worker_protocol_version,
        ),
        _require(
            f"{expected_profile}.baseline-candidates",
            "all eight runtime candidates are the exact clean baseline wheels and commits",
            len(record.candidates) == 8
            and not any(item.dirty for item in record.candidates)
            and candidate_commits == expected_commits
            and candidate_hashes == expected_hashes,
            candidate_count=len(record.candidates),
            dirty_candidates=[item.package for item in record.candidates if item.dirty],
            commits_match=candidate_commits == expected_commits,
            wheel_hashes_match=candidate_hashes == expected_hashes,
        ),
        _require(
            f"{expected_profile}.reports",
            "canonical JSON and deterministic Markdown/JUnit/config projections are retained",
            all(files.values()),
            files=files,
        ),
        _require(
            f"{expected_profile}.command-policy",
            "every command has an accepted exact classification",
            bool(record.commands)
            and all(item.classification in ACCEPTED_COMMAND_CLASSES for item in record.commands),
            classifications=_counts([item.classification for item in record.commands]),
        ),
    ]


def _install_requirements(record: RunRecord) -> list[RequirementResult]:
    expected = [step.id for step in profile("install-smoke").steps]
    actual = [item.id for item in record.commands]
    return [
        _require(
            "install-smoke.complete-47-check-contract",
            "the exact 47 installed-package checks all pass",
            actual == expected
            and len(actual) == 47
            and all(item.classification == "passed" for item in record.commands),
            expected_count=47,
            actual_count=len(actual),
            exact_ids=actual == expected,
            classifications=_counts([item.classification for item in record.commands]),
        )
    ]


def _historical_requirements(record: RunRecord) -> list[RequirementResult]:
    results = record.environment.get("historical_results", {})
    suites = results.get("suites", {}) if isinstance(results, dict) else {}
    known = [
        item for item in known_failures().failures if item.scope == "historical-regression"
    ]
    policy = record.known_failure_results
    observed_ids = [
        str(item.get("known_failure_id"))
        for item in policy
        if item.get("known_failure_id") is not None
    ]
    expected_ids = [item.id for item in known]
    failures = sum(
        int(item.get("failures", 0)) for item in suites.values() if isinstance(item, dict)
    )
    errors = sum(
        int(item.get("errors", 0)) for item in suites.values() if isinstance(item, dict)
    )
    expected_commands = [step.id for step in profile("historical-regression").steps]
    expected_tests = {
        "moseq2-extract": 70,
        "moseq2-pca": 30,
        "moseq2-model": 34,
        "moseq2-viz": 83,
        "moseq2-app": 1,
        "pybasicbayes": 34,
        "pyhsmm": 11,
        "pyhsmm-autoregressive": 1,
    }
    suite_counts_match = set(suites) == set(expected_tests) and all(
        isinstance(suites[name], dict) and suites[name].get("tests") == expected
        for name, expected in expected_tests.items()
    )
    return [
        _require(
            "historical-regression.complete-264-outcome-contract",
            "all eight historical suites account for exactly 264 test outcomes",
            results.get("full_run") is True
            and results.get("total_tests") == 264
            and [item.id for item in record.commands] == expected_commands
            and suite_counts_match
            and errors == 2
            and 3 <= failures <= 6,
            full_run=results.get("full_run"),
            total_tests=results.get("total_tests"),
            suite_count=len(suites),
            failures=failures,
            collection_errors=errors,
            per_suite_counts_match=suite_counts_match,
        ),
        _require(
            "historical-regression.exact-known-failure-policy",
            "every historical non-pass or allowed pass maps to one named policy entry",
            sorted(observed_ids) == sorted(expected_ids)
            and len(observed_ids) == len(set(observed_ids))
            and all(
                item.get("status") in {"known-failure", "allowed-pass"} for item in policy
            ),
            expected_ids=sorted(expected_ids),
            observed_ids=sorted(observed_ids),
            statuses=_counts([str(item.get("status")) for item in policy]),
        ),
    ]


def _pipeline_requirements(record: RunRecord) -> list[RequirementResult]:
    results = record.environment.get("pipeline_results", {})
    comparison_index = results.get("comparison_index", []) if isinstance(results, dict) else []
    required = [item for item in comparison_index if item.get("required_equal") is True]
    expected_commands = [step.id for step in profile("pipeline-smoke").steps]
    expected_failures = {
        item.id: 0 for item in known_failures().failures if item.scope == "pipeline-smoke"
    }
    for step in profile("pipeline-smoke").steps:
        if step.expected_failure:
            expected_failures[step.expected_failure] += 1
    policy_ids = [
        str(item.get("known_failure_id"))
        for item in record.known_failure_results
        if item.get("known_failure_id") is not None
    ]
    actual_failures = _counts(policy_ids)
    input_derivation = results.get("input_derivation", {}) if isinstance(results, dict) else {}
    scores = input_derivation.get("scores", []) if isinstance(input_derivation, dict) else []
    run_results = results.get("runs", {}) if isinstance(results, dict) else {}
    invariant_passes = [
        value.get("training_invariants", {}).get("passed") is True
        for value in run_results.values()
        if isinstance(value, dict)
    ]
    classifier = record.provenance.get("flip_classifier", {})
    expected_comparison_ids = [
        "extraction-h5",
        "extraction-yaml",
        "extraction-video",
        "pca-scores",
        "changepoints",
        "reference-model-application",
        "viz-dataframe",
        "crowd-movie",
        "app-smoke",
        "fresh-training-structure",
    ]
    comparison_index_valid = (
        [item.get("id") for item in comparison_index] == expected_comparison_ids
        and all(
            isinstance(item.get("result_index"), int)
            and 0 <= item["result_index"] < len(record.comparisons)
            and record.comparisons[item["result_index"]].status == item.get("status")
            for item in comparison_index
        )
    )
    return [
        _require(
            "pipeline-smoke.complete-28-step-contract",
            "both clean runs execute the exact accepted 28-step pipeline DAG",
            results.get("full_profile") is True
            and [item.id for item in record.commands] == expected_commands
            and results.get("expected_command_ids") == expected_commands,
            full_profile=results.get("full_profile"),
            command_count=len(record.commands),
            exact_ids=[item.id for item in record.commands] == expected_commands,
        ),
        _require(
            "pipeline-smoke.nine-semantic-equalities",
            "all nine required cross-run artifact comparisons are semantically equal",
            len(required) == 9
            and all(item.get("status") == "equal" for item in required)
            and len(record.comparisons) == 10
            and comparison_index_valid,
            required_count=len(required),
            required_statuses=_counts([str(item.get("status")) for item in required]),
            diagnostic_comparison_count=len(record.comparisons) - len(required),
            comparison_index_valid=comparison_index_valid,
        ),
        _require(
            "pipeline-smoke.exact-known-failure-policy",
            "the four named pipeline defects occur at their exact seven step locations",
            actual_failures == expected_failures
            and all(
                item.get("status") == "known-failure"
                and item.get("signature_matched") is True
                for item in record.known_failure_results
            ),
            expected_counts=expected_failures,
            observed_counts=actual_failures,
        ),
        _require(
            "pipeline-smoke.compact-real-data-contract",
            "the locked four-session/3,000-frame/25-component and 900-frame derivations are exact",
            len(scores) == 4
            and all(
                item.get("selected_frames") == 3000 and item.get("components") == 25
                for item in scores
            )
            and input_derivation.get("compatible_recording", {}).get("depth_frames") == 900
            and input_derivation.get("compatible_recording", {}).get("selected_timestamps")
            == 900,
            selected_uuids=input_derivation.get("selected_uuids"),
            scores=scores,
            compatible_recording=input_derivation.get("compatible_recording"),
        ),
        _require(
            "pipeline-smoke.explicit-flip-classifier",
            "extraction records the immutable flip-classifier logical ID and SHA-256",
            classifier.get("id")
            == "classifiers--flip-classifier-k2-c57-10to13weeks-pkl"
            and classifier.get("sha256")
            == "4b06e1e56928bb1ac227329d0932d4637cdd541a3af49865ae127b57991c2c00",
            classifier=classifier,
        ),
        _require(
            "pipeline-smoke.bounded-training-invariants",
            "both independent fresh training runs satisfy every bounded invariant",
            len(invariant_passes) == 2 and all(invariant_passes),
            run_count=len(invariant_passes),
            invariant_passes=invariant_passes,
        ),
        _require(
            "pipeline-smoke.local-dask-only",
            "the compact baseline uses one local Dask worker and submits no SLURM job",
            record.provenance.get("dask")
            == {"mode": "local", "workers": 1, "slurm_submitted": False},
            dask=record.provenance.get("dask"),
        ),
    ]


def evaluate_certification(
    suite_records: Mapping[str, tuple[RunRecord, Path, int]], baseline_lock: str
) -> list[RequirementResult]:
    """Evaluate the complete A-C contract without trusting aggregate suite totals."""

    requirements: list[RequirementResult] = []
    for suite_name, fixture_set_name in SUITE_MATRIX:
        record, run_directory, suite_exit_code = suite_records[suite_name]
        requirements.extend(
            _common_requirements(
                record,
                run_directory,
                expected_profile=suite_name,
                baseline_lock=baseline_lock,
                expected_fixture_sets=[fixture_set_name] if fixture_set_name else [],
                suite_exit_code=suite_exit_code,
            )
        )
        if suite_name == "install-smoke":
            requirements.extend(_install_requirements(record))
        elif suite_name == "historical-regression":
            requirements.extend(_historical_requirements(record))
        elif suite_name == "pipeline-smoke":
            requirements.extend(_pipeline_requirements(record))
    return requirements


def _manifest_identity(category: str, *parts: str) -> dict[str, Any]:
    with resource(category, *parts) as path:
        return {
            "logical_path": "/".join((category, *parts)),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }


def _copy_contract_manifests(destination: Path, fixture_sets: list[str]) -> list[dict[str, Any]]:
    specifications = [
        ("manifests", "sources", "moseq2-baseline-v1.yml"),
        ("manifests", "wheels", "moseq2-baseline-linux-py37-v1.yml"),
        ("manifests", "known-failures.yml"),
        *[("manifests", "fixtures", f"{name}.yml") for name in fixture_sets],
        ("profiles", "install-smoke.yml"),
        ("profiles", "historical-regression.yml"),
        ("profiles", "pipeline-smoke.yml"),
    ]
    identities: list[dict[str, Any]] = []
    for category, *parts in specifications:
        identity = _manifest_identity(category, *parts)
        identities.append(identity)
        with resource(category, *parts) as source:
            target = destination / category / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return identities


def _render_markdown(certification: Mapping[str, Any]) -> str:
    lines = [
        f"# MoSeq2 baseline certification `{certification['certification_id']}`",
        "",
        f"- Status: `{certification['status']}`",
        f"- Baseline lock: `{certification['baseline_lock']}`",
        f"- Framework: `{certification['framework_version']}`",
        f"- Suites: `{len(certification['suites'])}`",
        f"- Requirements: `{len(certification['requirements'])}`",
        "",
        "## Suite matrix",
        "",
        "| Profile | Status | Exit | Duration (s) | Run |",
        "|---|---|---:|---:|---|",
    ]
    for suite in certification["suites"]:
        lines.append(
            f"| `{suite['profile']}` | `{suite['status']}` | {suite['exit_code']} | "
            f"{suite['duration_seconds']:.3f} | `{suite['run_directory']}` |"
        )
    lines.extend(
        [
            "",
            "## Requirement-by-requirement parity",
            "",
            "| Requirement | Status | Description |",
            "|---|---|---|",
        ]
    )
    for requirement in certification["requirements"]:
        lines.append(
            f"| `{requirement['id']}` | **{requirement['status']}** | "
            f"{requirement['description']} |"
        )
    return "\n".join(lines) + "\n"


def _render_junit(certification: Mapping[str, Any]) -> bytes:
    requirements = certification["requirements"]
    failures = sum(item["status"] != "passed" for item in requirements)
    suite = ET.Element(
        "testsuite",
        name="baseline-certification",
        tests=str(len(requirements)),
        failures=str(failures),
        errors="0",
    )
    for requirement in requirements:
        case = ET.SubElement(
            suite, "testcase", name=requirement["id"], classname="baseline-certification"
        )
        if requirement["status"] != "passed":
            failure = ET.SubElement(case, "failure", message=requirement["description"])
            failure.text = json.dumps(requirement["evidence"], sort_keys=True)
    payload: bytes = ET.tostring(suite, encoding="utf-8", xml_declaration=True)
    return payload


def _write_certification(root: Path, certification: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "certification.json").write_text(
        json.dumps(certification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "certification.md").write_text(
        _render_markdown(certification), encoding="utf-8"
    )
    (root / "junit.xml").write_bytes(_render_junit(certification))
    (root / "metrics.json").write_text(
        json.dumps(certification["metrics"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def certify(
    *,
    baseline_lock: str,
    fixture_sets: list[str],
    cache_dir: Path,
    workspace: Path,
    output_dir: Path,
    executor: str,
    target_python: Path | None,
    container: str | None,
    offline: bool,
    runner: Callable[[Any], Any],
) -> Path:
    """Execute and certify the immutable install/history/pipeline matrix."""

    expected_fixture_sets = ["historical-v1", "pipeline-smoke-v1"]
    if len(fixture_sets) != len(expected_fixture_sets) or set(fixture_sets) != set(
        expected_fixture_sets
    ):
        raise InvalidConfiguration(
            "baseline certification requires exactly these fixture sets: "
            + ", ".join(expected_fixture_sets)
        )
    fixture_sets = expected_fixture_sets
    source_lock(baseline_lock)
    expected_object_hashes: set[str] = set()
    for name in fixture_sets:
        expected_object_hashes.update(item.sha256 for item in fixture_manifest(name).objects)

    started_at = datetime.now(UTC)
    certification_id = (
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-cert-{secrets.token_hex(4)}"
    )
    root = (output_dir / certification_id).resolve()
    suite_root = root / "runs"
    certification_workspace = (workspace / certification_id).resolve()
    root.mkdir(parents=True, exist_ok=False)
    suite_root.mkdir()
    certification_workspace.mkdir(parents=True, exist_ok=False)
    manifest_identities = _copy_contract_manifests(root / "contracts", fixture_sets)
    sampler = ResourceSampler(
        certification_workspace,
        root,
        cache_dir,
        expected_objects=len(expected_object_hashes),
    )
    sampler.start()
    suites: list[SuiteEvidence] = []
    records: dict[str, tuple[RunRecord, Path, int]] = {}
    execution_error: Moseq2TestError | None = None

    from moseq2_test.suites import RunOptions

    try:
        for suite_name, fixture_set_name in SUITE_MATRIX:
            suite_started = time.monotonic()
            destination = suite_root / suite_name
            destination.mkdir()
            try:
                result = runner(
                    RunOptions(
                        profile=suite_name,
                        packages=[],
                        sources=[],
                        candidates=[],
                        test_sources=[],
                        candidate_set=None,
                        baseline_lock=baseline_lock,
                        fixture_set=fixture_set_name,
                        intentional_change=None,
                        start_at=None,
                        through=None,
                        steps=[],
                        seed=0,
                        keep_sandbox="failure",
                        timeout=None,
                        jobs=1,
                        allow_dirty_source=False,
                        cache_dir=cache_dir,
                        workspace=certification_workspace,
                        output_dir=destination,
                        executor=executor,
                        target_python=target_python,
                        container=container,
                        offline=offline,
                    )
                )
                record = load_run(result.run_directory)
                relative = result.run_directory.relative_to(root).as_posix()
                suites.append(
                    SuiteEvidence(
                        profile=suite_name,
                        exit_code=result.exit_code,
                        run_directory=relative,
                        run_id=record.run_id,
                        status=record.status,
                        duration_seconds=time.monotonic() - suite_started,
                    )
                )
                records[suite_name] = (record, result.run_directory, result.exit_code)
            except Moseq2TestError as error:
                execution_error = error
                suites.append(
                    SuiteEvidence(
                        profile=suite_name,
                        exit_code=int(error.exit_code),
                        run_directory=destination.relative_to(root).as_posix(),
                        run_id=None,
                        status="error",
                        duration_seconds=time.monotonic() - suite_started,
                    )
                )
                break
            except Exception as error:
                execution_error = Moseq2TestError(
                    f"unexpected {type(error).__name__} while running {suite_name}: {error}"
                )
                suites.append(
                    SuiteEvidence(
                        profile=suite_name,
                        exit_code=int(ExitCode.INFRASTRUCTURE),
                        run_directory=destination.relative_to(root).as_posix(),
                        run_id=None,
                        status="error",
                        duration_seconds=time.monotonic() - suite_started,
                    )
                )
                break
    finally:
        metrics = sampler.stop()

    requirements: list[RequirementResult]
    if len(records) == len(SUITE_MATRIX):
        requirements = evaluate_certification(records, baseline_lock)
    else:
        completed = sorted(records)
        requirements = [
            _require(
                "baseline-certification.complete-suite-matrix",
                "install-smoke, historical-regression, and pipeline-smoke all produced records",
                False,
                completed_profiles=completed,
                missing_profiles=sorted(set(name for name, _ in SUITE_MATRIX) - set(completed)),
                error=str(execution_error) if execution_error else "runner did not return",
            )
        ]
    accepted = len(records) == len(SUITE_MATRIX) and all(
        item.status == "passed" for item in requirements
    )
    completed_at = datetime.now(UTC)
    certification: dict[str, Any] = {
        "schema_version": CERTIFICATION_VERSION,
        "certification_id": certification_id,
        "status": "accepted" if accepted else "failed",
        "failure_stage": None if accepted else "certification",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "framework_version": __version__,
        "worker_protocol_version": WORKER_PROTOCOL_VERSION,
        "baseline_lock": baseline_lock,
        "fixture_sets": fixture_sets,
        "executor": executor,
        "container": container,
        "offline": offline,
        "environment": controller_environment(),
        "contract_manifests": manifest_identities,
        "suites": [asdict(item) for item in suites],
        "requirements": [asdict(item) for item in requirements],
        "metrics": metrics,
        "failure_workspace_retained": not accepted,
    }
    _write_certification(root, certification)
    if accepted and certification_workspace.exists():
        shutil.rmtree(certification_workspace)
    elif certification_workspace.exists():
        (root / "failure-workspace.txt").write_text(
            str(certification_workspace) + "\n", encoding="utf-8"
        )
    if execution_error is not None:
        raise type(execution_error)(
            f"{execution_error}; certification retained at {root}"
        ) from execution_error
    if not accepted:
        failed = [item.id for item in requirements if item.status != "passed"]
        raise UnexpectedResult(
            f"baseline certification failed ({', '.join(failed)}); retained at {root}"
        )
    return root
