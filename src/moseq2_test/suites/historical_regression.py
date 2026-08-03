"""Migration of the eight immutable package-owned historical test suites."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from moseq2_test import WORKER_PROTOCOL_VERSION, __version__
from moseq2_test.candidates import build_sources, canonical_package, export_source, parse_assignment
from moseq2_test.data import CacheLayout, extract_object, fetch_selected, verify_selected
from moseq2_test.errors import ExitCode, InvalidConfiguration, MissingInput, Moseq2TestError
from moseq2_test.execution.process import execute_worker
from moseq2_test.execution.protocol import WorkerRequest
from moseq2_test.failure_policy import (
    classifications_accepted,
    classify_failures,
    junit_observations,
)
from moseq2_test.models import CandidateRecord, CommandResult, RunRecord, SourceRecord, SuiteProfile
from moseq2_test.provenance import controller_environment, redacted_environment, sha256_file
from moseq2_test.registry import fixture_manifest, known_failures, source_lock, wheel_lock
from moseq2_test.reporting import write_run_directory
from moseq2_test.sandbox import Sandbox
from moseq2_test.suites.install_smoke import (
    _artifact_roots,
    _baseline_candidates,
    _command_result,
    _create_layered_target,
    _explicit_candidates,
    _json,
    _run_id,
)

IMPORT_DIRECTORIES = {
    "moseq2-extract": "moseq2_extract",
    "moseq2-pca": "moseq2_pca",
    "moseq2-model": "moseq2_model",
    "moseq2-viz": "moseq2_viz",
    "moseq2-app": "moseq2_app",
    "pybasicbayes": "pybasicbayes",
    "pyhsmm": "pyhsmm",
    "pyhsmm-autoregressive": "autoregressive",
}
COLLECTION_NODEIDS = {
    "moseq2-app": "tests/gui_tests/test_main.py",
    "pyhsmm-autoregressive": "tests/test_distributions.py",
}
EXPECTED_TESTS = {
    "moseq2-extract": 70,
    "moseq2-pca": 30,
    "moseq2-model": 34,
    "moseq2-viz": 83,
    "moseq2-app": 1,
    "pybasicbayes": 34,
    "pyhsmm": 11,
    "pyhsmm-autoregressive": 1,
}
TEST_TOOL_REQUIREMENTS = (
    "pytest==5.4.1",
    "pytest-cov==2.5.1",
    "coverage==5.5",
    "py==1.11.0",
    "pluggy==0.13.1",
    "more-itertools==8.14.0",
)


def _parse_junit(path: Path) -> dict[str, int | float | bool]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    result: dict[str, int | float | bool] = {
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "time": 0.0,
        "parsed": True,
    }
    for suite in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            result[key] = int(result[key]) + int(suite.attrib.get(key, "0") or 0)
        result["time"] = float(result["time"]) + float(suite.attrib.get("time", "0") or 0)
    return result


def _selected_steps(profile: SuiteProfile, options: Any) -> list[Any]:
    if options.start_at or options.through:
        raise InvalidConfiguration(
            "--start-at/--through apply to pipeline profiles, not historical-regression"
        )
    steps = profile.steps
    if options.packages:
        requested = {canonical_package(name) for name in options.packages}
        known = {canonical_package(step.package or "") for step in steps}
        unknown = requested - known
        if unknown:
            raise InvalidConfiguration(f"unknown historical package filters: {sorted(unknown)}")
        steps = [step for step in steps if canonical_package(step.package or "") in requested]
    if options.steps:
        requested_steps = set(options.steps)
        known_steps = {step.id for step in profile.steps}
        unknown_steps = requested_steps - known_steps
        if unknown_steps:
            raise InvalidConfiguration(f"unknown historical steps: {sorted(unknown_steps)}")
        steps = [step for step in steps if step.id in requested_steps]
    if not steps:
        raise InvalidConfiguration("historical-regression selection is empty")
    return steps


def _source_overrides(values: list[str]) -> dict[str, Path]:
    overrides = dict(parse_assignment(value) for value in values)
    if len(overrides) != len(values):
        raise InvalidConfiguration("duplicate test-source package definitions")
    return overrides


def _git_output(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise InvalidConfiguration(f"cannot inspect Git source {path}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _locked_checkout(
    source: SourceRecord,
    *,
    mirror: Path | None,
    sandbox: Sandbox,
    offline: bool,
) -> Path:
    if mirror is not None:
        checkout = mirror / source.name
        if not checkout.exists():
            raise MissingInput(f"source mirror has no checkout for {source.name}: {checkout}")
    else:
        if offline:
            raise MissingInput(f"offline source mirror is required for {source.name}")
        checkout = sandbox.build / "source-clones" / source.name
        checkout.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["git", "clone", "--no-checkout", str(source.repository), str(checkout)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise MissingInput(f"could not clone {source.repository}: {completed.stderr.strip()}")
        completed = subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--detach", source.commit],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise MissingInput(f"could not check out {source.name}@{source.commit}")
    actual_commit = _git_output(checkout, "rev-parse", "HEAD")
    actual_tree = _git_output(checkout, "rev-parse", "HEAD^{tree}")
    if actual_commit != source.commit or actual_tree != source.tree:
        raise InvalidConfiguration(
            f"source identity mismatch for {source.name}: {actual_commit}/{actual_tree}"
        )
    if _git_output(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise InvalidConfiguration(f"locked source checkout is dirty: {checkout}")
    return checkout


def _make_mutable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


def _extract_locked_source_archive(
    source: SourceRecord,
    archive_path: Path,
    expected_sha256: str,
    destination: Path,
) -> dict[str, Any]:
    if not archive_path.is_file() or sha256_file(archive_path) != expected_sha256:
        raise MissingInput(f"missing or invalid locked source archive: {archive_path}")
    extraction_root = destination.parent / f".{destination.name}-archive"
    if extraction_root.exists() or destination.exists():
        raise InvalidConfiguration(f"source archive destination already exists: {destination}")
    extraction_root.mkdir()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > 20_000 or sum(item.size for item in members) > 2 * 1024**3:
                raise InvalidConfiguration(f"source archive exceeds safety ceiling: {archive_path}")
            for member in members:
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or not pure.parts
                    or pure.parts[0] != source.name
                    or not (member.isfile() or member.isdir())
                ):
                    raise InvalidConfiguration(
                        f"unsupported source archive member {member.name!r}: {archive_path}"
                    )
            archive.extractall(extraction_root, members=members, filter="data")
        extracted = extraction_root / source.name
        if not extracted.is_dir() or any(path != extracted for path in extraction_root.iterdir()):
            raise InvalidConfiguration(f"source archive has an unexpected root: {archive_path}")
        extracted.rename(destination)
    finally:
        if extraction_root.exists():
            shutil.rmtree(extraction_root)
    return {
        "commit": source.commit,
        "tree": source.tree,
        "dirty": False,
        "source_archive": archive_path.name,
        "source_archive_sha256": expected_sha256,
    }


def _runtime_bin(target_python: Path) -> Path | None:
    configuration = target_python.parent.parent / "pyvenv.cfg"
    if not configuration.is_file():
        return None
    for raw_line in configuration.read_text(encoding="utf-8").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip() == "home":
            return Path(value.strip())
    return None


def _stage_fixture(package: str, destination: Path, cache_dir: Path) -> None:
    if not package.startswith("moseq2-"):
        return
    manifest = fixture_manifest("historical-v1")
    item = next(value for value in manifest.objects if value.filename == f"{package}.zip")
    extracted = extract_object(CacheLayout(cache_dir), item)
    data = destination / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir()
    for child in extracted.iterdir():
        if child.name == ".moseq2-test-extracted.json":
            continue
        target = data / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    _make_mutable(data)


def _prepare_test_tree(
    package: str,
    source: SourceRecord,
    *,
    source_override: Path | None,
    source_mirror: Path | None,
    source_archive: tuple[Path, str] | None,
    sandbox: Sandbox,
    cache_dir: Path,
    offline: bool,
    allow_dirty: bool,
) -> tuple[Path, dict[str, Any]]:
    destination = sandbox.sources / package
    if source_override is not None:
        if (source_override / ".git").exists() or (source_override / ".git").is_file():
            commit, dirty = export_source(source_override, destination, allow_dirty=allow_dirty)
        else:
            if not source_override.is_dir():
                raise MissingInput(f"test source does not exist: {source_override}")
            shutil.copytree(source_override, destination)
            commit, dirty = "snapshot", False
    elif source_mirror is None and source_archive is not None and source_archive[0].is_file():
        staged = _extract_locked_source_archive(
            source, source_archive[0], source_archive[1], destination
        )
        commit, dirty = source.commit, False
    else:
        checkout = _locked_checkout(source, mirror=source_mirror, sandbox=sandbox, offline=offline)
        commit, dirty = export_source(checkout, destination, allow_dirty=False)
        staged = {"commit": commit, "dirty": dirty, "source_checkout": True}
    import_directory = destination / IMPORT_DIRECTORIES[package]
    if import_directory.exists():
        shutil.rmtree(import_directory)
    pytest_configuration = destination / "pytest.ini"
    if pytest_configuration.is_file():
        module = IMPORT_DIRECTORIES[package]
        configuration = pytest_configuration.read_text(encoding="utf-8")
        pytest_configuration.write_text(
            configuration.replace(f"--cov={module}/", f"--cov={module}"),
            encoding="utf-8",
        )
    _stage_fixture(package, destination, cache_dir)
    if source_override is not None:
        staged = {"commit": commit, "dirty": dirty, "source_override": True}
    return destination, {**staged, "import_root_removed": True}


def _install_test_tools(target_python: Path, base_prefix: Path, timeout: int) -> None:
    mirror = Path(
        os.environ.get(
            "MOSEQ2_TEST_TEST_TOOL_WHEEL_MIRROR", base_prefix.parent / "test_tool_wheels"
        )
    )
    wheels = sorted(mirror.glob("*.whl"))
    if len(wheels) != len(TEST_TOOL_REQUIREMENTS):
        raise MissingInput(
            f"expected {len(TEST_TOOL_REQUIREMENTS)} locked test-tool wheels in {mirror}"
        )
    completed = subprocess.run(
        [
            str(target_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            *map(str, wheels),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise InvalidConfiguration(f"could not install historical test tools: {completed.stderr}")


def _test_command(step: Any, python: Path, junit: Path) -> list[str]:
    if step.operation == "pytest":
        return [str(python), "-m", "pytest", f"--junitxml={junit}"]
    selector = list(step.command[4:])
    return [
        str(python),
        "-m",
        "nose",
        "tests",
        *selector,
        "--with-xunit",
        f"--xunit-file={junit}",
    ]


def _run_suite(
    package: str,
    step: Any,
    *,
    target_python: Path,
    test_tree: Path,
    sandbox: Sandbox,
    timeout: int,
    environment: dict[str, str],
    baseline_commits: dict[str, str],
) -> tuple[CommandResult, dict[str, Any], list[dict[str, Any]]]:
    junit = sandbox.result / f"{package}.xml"
    command = _test_command(step, target_python, junit)
    started = time.monotonic()
    response = execute_worker(
        target_python,
        WorkerRequest(
            request_id=step.id,
            operation="run-command",
            parameters={
                "command": command,
                "cwd": str(test_tree),
                "environment": environment,
                "unset_environment": ["DISPLAY", "PYTHONPATH", "PYTHONHOME"],
                "timeout": timeout,
            },
        ),
        work=sandbox.work,
        timeout=timeout + 30,
    )
    duration = time.monotonic() - started
    if response.result is None:
        raise InvalidConfiguration(f"historical worker returned no result for {package}")
    raw = response.result
    log = sandbox.result / f"{package}.log"
    log.write_text(str(raw.get("stdout", "")) + str(raw.get("stderr", "")), encoding="utf-8")
    if not junit.is_file():
        return (
            _command_result(
                step.id,
                False,
                {"error": "test runner produced no JUnit", "raw_returncode": raw.get("returncode")},
                command=command,
                returncode=int(raw.get("returncode", 1)),
                duration=duration,
            ),
            {"parsed": False, "tests": 0, "failures": 0, "errors": 0, "skipped": 0},
            [],
        )
    summary = _parse_junit(junit)
    observations = junit_observations(
        junit,
        scope="historical-regression",
        package=package,
        collection_nodeid=COLLECTION_NODEIDS.get(package),
    )
    classifications = classify_failures(
        observations,
        known_failures(),
        scope="historical-regression",
        package=package,
        baseline_commits=baseline_commits,
    )
    classification_records = [
        {
            "package": package,
            "target": value.target,
            "status": value.status,
            "known_failure_id": value.known_failure_id,
            "message": value.message,
        }
        for value in classifications
    ]
    counts_ok = int(summary["tests"]) == EXPECTED_TESTS[package]
    accepted = counts_ok and classifications_accepted(classifications)
    if not accepted:
        classification = "failed"
    elif any(value.status == "known-failure" for value in classifications):
        classification = "known-failure"
    elif any(value.status == "allowed-pass" for value in classifications):
        classification = "allowed-pass"
    else:
        classification = "passed"
    result = CommandResult(
        id=step.id,
        command=command,
        returncode=int(raw.get("returncode", 1)),
        duration_seconds=duration,
        stdout=_json(
            {
                "package": package,
                "junit": summary,
                "expected_tests": EXPECTED_TESTS[package],
                "counts_match": counts_ok,
                "policy": classification_records,
            }
        ),
        stderr=None,
        classification=classification,
    )
    return result, summary, classification_records


def _copy_evidence(sandbox: Sandbox, run_directory: Path) -> None:
    logs = run_directory / "logs"
    junit = run_directory / "junit"
    logs.mkdir(exist_ok=True)
    junit.mkdir(exist_ok=True)
    for path in sandbox.result.glob("*.log"):
        shutil.copy2(path, logs / path.name)
    for path in sandbox.result.glob("*.xml"):
        shutil.copy2(path, junit / path.name)
    for path in sandbox.work.glob("*.response.json"):
        shutil.copy2(path, logs / path.name)
    for path in sandbox.work.glob("*.request.json"):
        shutil.copy2(path, logs / path.name)


def run_historical_regression(options: Any, profile: SuiteProfile) -> tuple[Path, int]:
    started_at = datetime.now(UTC)
    run_id = _run_id(started_at)
    run_directory = (options.output_dir / run_id).resolve()
    selected_steps = _selected_steps(profile, options)
    timeout = options.timeout or profile.resources.timeout_seconds
    if timeout > profile.resources.timeout_seconds:
        raise InvalidConfiguration("--timeout cannot exceed the profile certification ceiling")
    if options.keep_sandbox not in {"always", "failure", "never"}:
        raise InvalidConfiguration("--keep-sandbox must be always, failure, or never")
    if options.executor != "process":
        raise InvalidConfiguration("historical container execution is unavailable until P10")
    if options.target_python is None:
        raise MissingInput("historical process execution requires --target-python")
    # A venv's Python is commonly a symlink. Keep the venv path so pyvenv.cfg
    # and the locked test-tool overlay remain active.
    base_python = options.target_python.expanduser().absolute()
    if not base_python.is_file():
        raise MissingInput(f"target Python does not exist: {base_python}")
    if options.fixture_set not in {None, "historical-v1"}:
        raise InvalidConfiguration("historical-regression requires fixture set historical-v1")

    sandbox = Sandbox.create(options.workspace, prefix="moseq2-test-historical-")
    source_manifest = source_lock(options.baseline_lock)
    source_by_name = {item.name: item for item in source_manifest.sources}
    wheel_manifest = wheel_lock("moseq2-baseline-linux-py37-v1")
    baseline_commits = {item.name: item.commit for item in source_manifest.sources}
    commands: list[CommandResult] = []
    policy_results: list[dict[str, Any]] = []
    junit_summaries: dict[str, dict[str, Any]] = {}
    resolved_candidates: list[CandidateRecord] = []
    source_provenance: dict[str, Any] = {}
    failure_stage: str | None = None
    environment: dict[str, Any] = {"controller": controller_environment()}
    provenance: dict[str, Any] = {
        "environment_variables": redacted_environment(),
        "selected_steps": [step.id for step in selected_steps],
    }
    try:
        base_prefix = base_python.parent.parent.resolve()
        roots = _artifact_roots(base_prefix)
        baseline_candidates = _baseline_candidates(wheel_manifest, roots["wheel"])
        candidate_records = _explicit_candidates(options.candidates, options.candidate_set)
        if options.sources:
            built = build_sources(
                options.sources,
                workspace=sandbox.build / "candidate-workspace",
                output=sandbox.build / "candidate-output",
                allow_dirty=options.allow_dirty_source,
                build_python=base_python,
            )
            candidate_records.extend(built.candidates)
        candidate_names = [canonical_package(item.package) for item in candidate_records]
        if len(candidate_names) != len(set(candidate_names)):
            raise InvalidConfiguration("duplicate historical candidate packages")
        candidate_by_name = {canonical_package(item.package): item for item in candidate_records}
        resolved_candidates = [
            candidate_by_name.get(canonical_package(item.package), item)
            for item in baseline_candidates
        ]
        target_python = _create_layered_target(
            base_python, sandbox, candidate_records, timeout, force=True
        )
        _install_test_tools(target_python, base_prefix, timeout)

        fixture_mirror_value = os.environ.get("MOSEQ2_TEST_FIXTURE_MIRROR")
        fixture_mirror = Path(fixture_mirror_value) if fixture_mirror_value else None
        fetch_selected(
            options.cache_dir,
            profile_name="historical-regression",
            fixture_sets=[],
            mirror=fixture_mirror,
            offline=options.offline,
        )
        source_mirror_value = os.environ.get("MOSEQ2_TEST_SOURCE_MIRROR")
        source_mirror = Path(source_mirror_value) if source_mirror_value else None
        wheel_by_name = {item.package: item for item in wheel_manifest.wheels}
        test_overrides = _source_overrides(options.test_sources)
        for candidate in candidate_records:
            if candidate.test_source:
                test_overrides.setdefault(
                    canonical_package(candidate.package), Path(candidate.test_source)
                )
        for value in options.sources:
            name, path = parse_assignment(value)
            test_overrides.setdefault(name, path)

        path_entries = [str(target_python.parent)]
        runtime_bin = _runtime_bin(target_python)
        if runtime_bin is not None:
            path_entries.append(str(runtime_bin))
        path_entries.append(os.environ.get("PATH", ""))
        test_environment = {
            "PATH": os.pathsep.join(path_entries),
            "PYTHONNOUSERSITE": "1",
            "PYTHONHASHSEED": str(options.seed),
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(sandbox.work / "matplotlib"),
            "QT_QPA_PLATFORM": "offscreen",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "COVERAGE_FILE": str(sandbox.work / ".coverage"),
        }
        for step in selected_steps:
            package = canonical_package(step.package or "")
            source = source_by_name[package]
            source_wheel = wheel_by_name[package]
            test_tree, staged = _prepare_test_tree(
                package,
                source,
                source_override=test_overrides.get(package),
                source_mirror=source_mirror,
                source_archive=(
                    roots["source_archive"] / source_wheel.source_archive_filename,
                    source_wheel.source_archive_sha256,
                ),
                sandbox=sandbox,
                cache_dir=options.cache_dir,
                offline=options.offline,
                allow_dirty=options.allow_dirty_source,
            )
            source_provenance[package] = staged
            result, junit_summary, classifications = _run_suite(
                package,
                step,
                target_python=target_python,
                test_tree=test_tree,
                sandbox=sandbox,
                timeout=timeout,
                environment=test_environment,
                baseline_commits=baseline_commits,
            )
            commands.append(result)
            junit_summaries[package] = junit_summary
            policy_results.extend(classifications)
        verify_selected(options.cache_dir, profile_name="historical-regression", fixture_sets=[])
        environment["target"] = {
            "python": str(target_python),
            "test_tools": list(TEST_TOOL_REQUIREMENTS),
            "variables": test_environment,
        }
        provenance["sources"] = source_provenance
        provenance["display_paths"] = {
            "base_python": str(base_python),
            "target_python": str(target_python),
            "source_mirror": str(source_mirror) if source_mirror else None,
            "fixture_mirror": str(fixture_mirror) if fixture_mirror else None,
        }
    except Moseq2TestError as error:
        failure_stage = "setup"
        commands.append(
            _command_result(
                "setup",
                False,
                {"error_type": type(error).__name__, "error": str(error), "code": error.code},
            )
        )
    except Exception as error:
        failure_stage = "infrastructure"
        commands.append(
            _command_result(
                "infrastructure",
                False,
                {"error_type": type(error).__name__, "error": str(error)},
            )
        )

    accepted_classes = {"passed", "known-failure", "allowed-pass"}
    accepted = not failure_stage and all(
        command.classification in accepted_classes for command in commands
    )
    full_run = len(selected_steps) == len(profile.steps)
    total_tests = sum(int(value.get("tests", 0)) for value in junit_summaries.values())
    if full_run and total_tests != 264:
        accepted = False
        failure_stage = failure_stage or "test"
    if not accepted and failure_stage is None:
        failure_stage = "test"
    environment["historical_results"] = {
        "suites": junit_summaries,
        "total_tests": total_tests,
        "expected_total_for_full_run": 264,
        "full_run": full_run,
    }
    record = RunRecord(
        run_id=run_id,
        framework_version=__version__,
        worker_protocol_version=WORKER_PROTOCOL_VERSION,
        profile=profile.name,
        status="accepted" if accepted else "failed",
        failure_stage=failure_stage,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        seed=options.seed,
        source_lock=source_manifest.lock_id,
        wheel_lock=wheel_manifest.lock_id,
        fixture_sets=["historical-v1"],
        candidates=resolved_candidates,
        commands=commands,
        known_failure_results=policy_results,
        environment=environment,
        provenance=provenance,
    )
    write_run_directory(
        run_directory,
        record,
        resolved_config={
            "profile": profile.name,
            "packages": [step.package for step in selected_steps],
            "steps": [step.id for step in selected_steps],
            "baseline_lock": options.baseline_lock,
            "fixture_set": "historical-v1",
            "seed": options.seed,
        },
    )
    (run_directory / "source-lock.json").write_text(
        _json(source_manifest.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "wheel-lock.json").write_text(
        _json(wheel_manifest.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "fixture-manifest.json").write_text(
        _json(fixture_manifest("historical-v1").model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "manifests" / "suite-profile.json").write_text(
        _json(profile.model_dump(mode="json")), encoding="utf-8"
    )
    _copy_evidence(sandbox, run_directory)
    retain = options.keep_sandbox == "always" or (
        options.keep_sandbox == "failure" and not accepted
    )
    if retain:
        (run_directory / "sandbox.txt").write_text(str(sandbox.root) + "\n", encoding="utf-8")
    else:
        sandbox.cleanup()
    return run_directory, int(ExitCode.ACCEPTED if accepted else ExitCode.DIFFERENCE)
