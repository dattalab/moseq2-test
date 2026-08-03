from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from moseq2_test import WORKER_PROTOCOL_VERSION, __version__
from moseq2_test.certification import evaluate_certification
from moseq2_test.models import (
    CandidateKind,
    CandidateRecord,
    CommandResult,
    ComparisonResult,
    RunRecord,
)
from moseq2_test.registry import known_failures, profile, source_lock, wheel_lock
from moseq2_test.reporting import write_run_directory


def _candidates() -> list[CandidateRecord]:
    source_by_name = {item.name: item for item in source_lock("moseq2-baseline-v1").sources}
    return [
        CandidateRecord(
            package=item.package,
            kind=CandidateKind.WHEEL,
            location=f"wheelhouse/{item.filename}",
            sha256=item.sha256,
            source_commit=source_by_name[item.package].commit,
        )
        for item in wheel_lock("moseq2-baseline-linux-py37-v1").wheels
    ]


def _command(identifier: str, classification: str = "passed") -> CommandResult:
    return CommandResult(
        id=identifier,
        command=["synthetic", identifier],
        returncode=0,
        duration_seconds=0,
        classification=classification,
    )


def _record(profile_name: str) -> RunRecord:
    selected = profile(profile_name)
    commands = [_command(step.id) for step in selected.steps]
    fixture_sets: list[str] = []
    environment: dict[str, object] = {}
    provenance: dict[str, object] = {}
    policy: list[dict[str, object]] = []
    comparisons: list[ComparisonResult] = []
    if profile_name == "historical-regression":
        fixture_sets = ["historical-v1"]
        policy = [
            {
                "known_failure_id": item.id,
                "status": "allowed-pass" if item.policy == "allowed_failure" else "known-failure",
            }
            for item in known_failures().failures
            if item.scope == profile_name
        ]
        commands[0] = _command(commands[0].id, "known-failure")
        environment = {
            "historical_results": {
                "full_run": True,
                "total_tests": 264,
                "suites": {
                    "moseq2-extract": {
                        "tests": 70,
                        "failures": 2,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "moseq2-pca": {"tests": 30, "failures": 0, "errors": 0, "skipped": 0},
                    "moseq2-model": {
                        "tests": 34,
                        "failures": 0,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "moseq2-viz": {"tests": 83, "failures": 1, "errors": 0, "skipped": 0},
                    "moseq2-app": {"tests": 1, "failures": 0, "errors": 1, "skipped": 0},
                    "pybasicbayes": {
                        "tests": 34,
                        "failures": 0,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "pyhsmm": {"tests": 11, "failures": 0, "errors": 0, "skipped": 0},
                    "pyhsmm-autoregressive": {
                        "tests": 1,
                        "failures": 0,
                        "errors": 1,
                        "skipped": 0,
                    },
                },
            }
        }
    elif profile_name == "pipeline-smoke":
        fixture_sets = ["pipeline-smoke-v1"]
        policy = []
        expected_counts: dict[str, int] = {}
        for step in selected.steps:
            if step.expected_failure:
                commands[[item.id for item in commands].index(step.id)] = _command(
                    step.id, "known-failure"
                )
                expected_counts[step.expected_failure] = (
                    expected_counts.get(step.expected_failure, 0) + 1
                )
                policy.append(
                    {
                        "known_failure_id": step.expected_failure,
                        "status": "known-failure",
                        "signature_matched": True,
                    }
                )
        comparison_index = []
        comparison_ids = [
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
        for index in range(10):
            required = index < 9
            status = "equal" if required else "different"
            comparisons.append(
                ComparisonResult(
                    status=status,
                    kind=comparison_ids[index],
                    comparator="synthetic",
                    comparator_version="1",
                    policy="synthetic-v1",
                    summary=status,
                )
            )
            comparison_index.append(
                {
                    "id": comparison_ids[index],
                    "required_equal": required,
                    "status": status,
                    "result_index": index,
                }
            )
        environment = {
            "pipeline_results": {
                "full_profile": True,
                "expected_command_ids": [step.id for step in selected.steps],
                "comparison_index": comparison_index,
                "expected_known_failure_counts": expected_counts,
                "input_derivation": {
                    "selected_uuids": ["a", "b", "c", "d"],
                    "scores": [
                        {"uuid": value, "selected_frames": 3000, "components": 25}
                        for value in ("a", "b", "c", "d")
                    ],
                    "compatible_recording": {"depth_frames": 900, "selected_timestamps": 900},
                },
                "runs": {
                    "run-1": {"training_invariants": {"passed": True}},
                    "run-2": {"training_invariants": {"passed": True}},
                },
            }
        }
        provenance = {
            "flip_classifier": {
                "id": "classifiers--flip-classifier-k2-c57-10to13weeks-pkl",
                "sha256": (
                    "4b06e1e56928bb1ac227329d0932d4637cdd541a3af49865ae127b57991c2c00"
                ),
            },
            "dask": {"mode": "local", "workers": 1, "slurm_submitted": False},
        }
    now = datetime.now(UTC)
    return RunRecord(
        run_id=f"synthetic-{profile_name}",
        framework_version=__version__,
        worker_protocol_version=WORKER_PROTOCOL_VERSION,
        profile=profile_name,
        status="accepted",
        started_at=now,
        completed_at=now,
        seed=0,
        source_lock="moseq2-baseline-v1",
        wheel_lock="moseq2-baseline-linux-py37-v1",
        fixture_sets=fixture_sets,
        candidates=_candidates(),
        commands=commands,
        comparisons=comparisons,
        known_failure_results=policy,
        environment=environment,
        provenance=provenance,
    )


def _suite_records(tmp_path: Path) -> dict[str, tuple[RunRecord, Path, int]]:
    records: dict[str, tuple[RunRecord, Path, int]] = {}
    for profile_name in ("install-smoke", "historical-regression", "pipeline-smoke"):
        record = _record(profile_name)
        root = tmp_path / profile_name
        write_run_directory(root, record)
        records[profile_name] = (record, root, 0)
    return records


def test_complete_exact_matrix_certifies(tmp_path: Path) -> None:
    requirements = evaluate_certification(_suite_records(tmp_path), "moseq2-baseline-v1")
    assert len(requirements) == 25
    assert all(item.status == "passed" for item in requirements)


def test_semantic_difference_fails_certification(tmp_path: Path) -> None:
    records = _suite_records(tmp_path)
    record, root, exit_code = records["pipeline-smoke"]
    comparisons = list(record.comparisons)
    comparisons[0] = comparisons[0].model_copy(update={"status": "different"})
    pipeline_results = dict(record.environment["pipeline_results"])
    comparison_index = [dict(item) for item in pipeline_results["comparison_index"]]
    comparison_index[0]["status"] = "different"
    pipeline_results["comparison_index"] = comparison_index
    records["pipeline-smoke"] = (
        record.model_copy(
            update={
                "comparisons": comparisons,
                "environment": {"pipeline_results": pipeline_results},
            }
        ),
        root,
        exit_code,
    )
    requirements = evaluate_certification(records, "moseq2-baseline-v1")
    failed = {item.id for item in requirements if item.status == "failed"}
    assert failed == {"pipeline-smoke.nine-semantic-equalities"}


def test_unknown_failure_and_unexpected_pass_fail_certification(tmp_path: Path) -> None:
    records = _suite_records(tmp_path)
    record, root, exit_code = records["historical-regression"]
    policy = [dict(item) for item in record.known_failure_results]
    policy[0]["known_failure_id"] = "unregistered-defect"
    policy[1]["status"] = "unexpected-pass"
    records["historical-regression"] = (
        record.model_copy(update={"known_failure_results": policy}),
        root,
        exit_code,
    )
    requirements = evaluate_certification(records, "moseq2-baseline-v1")
    failed = {item.id for item in requirements if item.status == "failed"}
    assert failed == {"historical-regression.exact-known-failure-policy"}
