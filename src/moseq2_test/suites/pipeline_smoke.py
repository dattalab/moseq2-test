"""Compact, real-data MoSeq2 pipeline fixtures and execution."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py  # type: ignore[import-untyped]
import yaml

from moseq2_test import WORKER_PROTOCOL_VERSION, __version__
from moseq2_test.candidates import build_sources, canonical_package
from moseq2_test.compare.registry import compare, load_intentional_change
from moseq2_test.data import CacheLayout, extract_object, fetch_selected
from moseq2_test.errors import ExitCode, InvalidConfiguration, MissingInput, Moseq2TestError
from moseq2_test.execution.process import execute_worker
from moseq2_test.execution.protocol import WorkerRequest
from moseq2_test.models import (
    CandidateRecord,
    CommandResult,
    ComparisonResult,
    ComparisonStatus,
    FixtureManifest,
    FixtureObject,
    KnownFailure,
    RunRecord,
    SuiteProfile,
)
from moseq2_test.provenance import controller_environment, redacted_environment
from moseq2_test.registry import fixture_manifest, known_failures, source_lock, wheel_lock
from moseq2_test.reporting import write_run_directory
from moseq2_test.sandbox import Sandbox, assert_unchanged, snapshot_tree
from moseq2_test.suites.install_smoke import (
    _apply_test_sources,
    _artifact_roots,
    _baseline_candidates,
    _create_layered_target,
    _explicit_candidates,
    _install_records,
    _json,
)

FRAME_LIMIT = 3000
SELECTED_UUIDS = (
    "8285b30c-97b8-46f0-8d38-a140f39ca99d",
    "979dd038-c828-4ecc-bd2e-5aa56f7d7bf6",
    "9cf525d7-575b-4143-8141-0842bf7d2860",
    "93bb4988-d35c-44d9-a3be-54a6e878b4c9",
)


@dataclass(frozen=True)
class PipelineInputs:
    root: Path
    raw_good: Path
    raw_bad: Path
    compatible_good: Path
    classifier: Path
    aggregate_results: Path
    pca_components: Path
    pca_configuration: Path
    reduced_scores: Path
    legacy_model: Path
    index: Path
    derivation: dict[str, Any]


@dataclass
class PipelineRunState:
    number: int
    root: Path
    index: Path
    commands: list[CommandResult]
    known_failure_results: list[dict[str, Any]]
    training_invariants: dict[str, Any] = field(default_factory=dict)
    model_summaries: dict[str, Path] = field(default_factory=dict)
    specific_syllable: int | None = None


def _object_by_id(manifest: FixtureManifest, object_id: str) -> FixtureObject:
    try:
        return next(item for item in manifest.objects if item.id == object_id)
    except StopIteration as error:
        raise InvalidConfiguration(f"pipeline fixture manifest has no {object_id}") from error


def _cached_object(layout: CacheLayout, manifest: FixtureManifest, object_id: str) -> Path:
    item = _object_by_id(manifest, object_id)
    return layout.object_path(item.sha256)


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _copy_dataset(source: h5py.Dataset, destination: h5py.Group, name: str, limit: int) -> None:
    data = source[:limit]
    options: dict[str, Any] = {}
    if source.compression is not None:
        options["compression"] = source.compression
        options["compression_opts"] = source.compression_opts
    if source.shuffle:
        options["shuffle"] = True
    if source.fletcher32:
        options["fletcher32"] = True
    created = destination.create_dataset(name, data=data, **options)
    for key, value in source.attrs.items():
        created.attrs[key] = value


def make_reduced_extraction(source_path: Path, destination_path: Path) -> None:
    """Retain the first 3,000 frames and all non-frame metadata."""
    with h5py.File(source_path, "r") as source, h5py.File(destination_path, "w") as destination:
        for key, value in source.attrs.items():
            destination.attrs[key] = value
        for name, value in source.items():
            if name == "metadata":
                source.copy(value, destination, name=name)
            elif isinstance(value, h5py.Group):
                group = destination.create_group(name)
                for key, attribute in value.attrs.items():
                    group.attrs[key] = attribute
                for dataset_name, dataset in value.items():
                    if not isinstance(dataset, h5py.Dataset):
                        raise InvalidConfiguration(
                            f"unsupported nested group in extraction fixture: {name}/{dataset_name}"
                        )
                    _copy_dataset(dataset, group, dataset_name, FRAME_LIMIT)
            elif isinstance(value, h5py.Dataset):
                _copy_dataset(value, destination, name, FRAME_LIMIT)
            else:  # pragma: no cover - h5py currently exposes groups/datasets here
                source.copy(value, destination, name=name)


def make_reduced_scores(source_path: Path, destination_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with h5py.File(source_path, "r") as source, h5py.File(destination_path, "w") as destination:
        for key, value in source.attrs.items():
            destination.attrs[key] = value
        scores = destination.create_group("scores")
        indices = destination.create_group("scores_idx")
        metadata = destination.create_group("metadata")
        for uuid in SELECTED_UUIDS:
            source_scores = source["scores"][uuid]
            source_indices = source["scores_idx"][uuid]
            count = min(FRAME_LIMIT, source_scores.shape[0], source_indices.shape[0])
            _copy_dataset(source_scores, scores, uuid, count)
            _copy_dataset(source_indices, indices, uuid, count)
            source.copy(source["metadata"][uuid], metadata, name=uuid)
            records.append(
                {
                    "uuid": uuid,
                    "source_frames": int(source_scores.shape[0]),
                    "selected_frames": int(count),
                    "components": int(source_scores.shape[1]),
                }
            )
    return records


def make_compatible_recording(source: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True)
    _link_or_copy(source / "depth.dat", destination / "depth.dat")
    shutil.copy2(source / "metadata.json", destination / "metadata.json")
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    width, height = metadata["DepthResolution"]
    frame_count = (source / "depth.dat").stat().st_size // (int(width) * int(height) * 2)
    timestamps = (source / "depth_ts.txt").read_text(encoding="utf-8").splitlines()
    if len(timestamps) < frame_count:
        raise InvalidConfiguration("good-session timestamps are shorter than its depth clip")
    (destination / "depth_ts.txt").write_text(
        "\n".join(timestamps[:frame_count]) + "\n", encoding="utf-8"
    )
    return {
        "recipe": "compatible-good-v1",
        "depth_frames": int(frame_count),
        "source_timestamps": len(timestamps),
        "selected_timestamps": int(frame_count),
    }


def _session_archive(
    layout: CacheLayout, manifest: FixtureManifest, object_id: str, directory: str
) -> Path:
    item = _object_by_id(manifest, object_id)
    root = extract_object(layout, item)
    session = root / directory
    if not session.is_dir():
        raise InvalidConfiguration(f"recording archive {object_id} has no {directory}/ root")
    return session


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def derive_pipeline_inputs(
    cache_dir: Path,
    destination: Path,
    *,
    mirror: Path | None,
    offline: bool,
) -> PipelineInputs:
    """Fetch, verify, and derive the immutable compact pipeline input project."""
    if destination.exists() and any(destination.iterdir()):
        raise InvalidConfiguration(f"pipeline input destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    fetch_selected(
        cache_dir,
        profile_name="pipeline-smoke",
        fixture_sets=[],
        mirror=mirror,
        offline=offline,
    )
    manifest = fixture_manifest("pipeline-smoke-v1")
    layout = CacheLayout(cache_dir)
    raw_good = _session_archive(
        layout, manifest, "selected-recordings--good-session-tar-gz", "good-session"
    )
    raw_bad = _session_archive(
        layout, manifest, "selected-recordings--bad-session-tar-gz", "bad-session"
    )
    compatible = destination / "compatible_recordings" / "timestamp_compatible_good_session"
    compatible_record = make_compatible_recording(raw_good, compatible)

    classifier = destination / "classifiers" / "flip_classifier_k2_c57_10to13weeks.pkl"
    _link_or_copy(
        _cached_object(layout, manifest, "classifiers--flip-classifier-k2-c57-10to13weeks-pkl"),
        classifier,
    )
    aggregate = destination / "aggregate_results"
    aggregate.mkdir()
    source_index = yaml.safe_load(
        _cached_object(layout, manifest, "downstream-source--moseq2-index-yaml").read_text(
            encoding="utf-8"
        )
    )
    by_uuid = {entry["uuid"]: entry for entry in source_index["files"]}
    missing = sorted(set(SELECTED_UUIDS) - set(by_uuid))
    if missing:
        raise InvalidConfiguration(f"selected pipeline UUIDs are missing: {missing}")
    selected_entries: list[dict[str, Any]] = []
    for uuid in SELECTED_UUIDS:
        entry = copy.deepcopy(by_uuid[uuid])
        filename = entry["filename"]
        stem = filename.removesuffix(".h5")
        h5_item = next(
            item
            for item in manifest.objects
            if item.filename == filename and "aggregate-results" in item.id
        )
        yaml_item = next(
            item
            for item in manifest.objects
            if item.filename == f"{stem}.yaml" and "aggregate-results" in item.id
        )
        make_reduced_extraction(layout.object_path(h5_item.sha256), aggregate / filename)
        _link_or_copy(layout.object_path(yaml_item.sha256), aggregate / yaml_item.filename)
        entry["path"] = [
            f"./aggregate_results/{filename}",
            f"./aggregate_results/{yaml_item.filename}",
        ]
        selected_entries.append(entry)

    pca = destination / "_pca"
    pca.mkdir()
    pca_components = pca / "pca.h5"
    pca_configuration = pca / "pca.yaml"
    _link_or_copy(
        _cached_object(layout, manifest, "downstream-source---pca--pca-h5"), pca_components
    )
    _link_or_copy(
        _cached_object(layout, manifest, "downstream-source---pca--pca-yaml"),
        pca_configuration,
    )
    reduced_scores = pca / "pca_scores_reduced.h5"
    score_records = make_reduced_scores(
        _cached_object(layout, manifest, "downstream-source---pca--pca-scores-h5"),
        reduced_scores,
    )
    legacy_model = destination / "models" / "model-000-1000.p"
    _link_or_copy(
        _cached_object(layout, manifest, "downstream-source--models--model-000-1000-p"),
        legacy_model,
    )
    _link_or_copy(
        _cached_object(layout, manifest, "downstream-source--config-yaml"),
        destination / "config.yaml",
    )
    index = destination / "moseq2-index.yaml"
    index.write_text(
        yaml.safe_dump(
            {"files": selected_entries, "pca_path": "./_pca/pca_scores_reduced.h5"},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    derivation = {
        "schema_version": 1,
        "recipes": ["compatible-good-v1", "pipeline-smoke-reductions-v1"],
        "frame_limit_per_session": FRAME_LIMIT,
        "selected_uuids": list(SELECTED_UUIDS),
        "compatible_recording": compatible_record,
        "scores": score_records,
        "flip_classifier": {
            "id": "classifiers--flip-classifier-k2-c57-10to13weeks-pkl",
            "sha256": _object_by_id(
                manifest, "classifiers--flip-classifier-k2-c57-10to13weeks-pkl"
            ).sha256,
        },
    }
    (destination / "derivation.json").write_text(
        json.dumps(derivation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _make_read_only(destination)
    return PipelineInputs(
        root=destination,
        raw_good=raw_good,
        raw_bad=raw_bad,
        compatible_good=compatible,
        classifier=classifier,
        aggregate_results=aggregate,
        pca_components=pca_components,
        pca_configuration=pca_configuration,
        reduced_scores=reduced_scores,
        legacy_model=legacy_model,
        index=index,
        derivation=derivation,
    )


def _pipeline_environment(target_python: Path, sandbox: Sandbox, seed: int) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join([str(target_python.parent), os.environ.get("PATH", "")]),
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": str(seed),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(sandbox.work / "matplotlib"),
        "QT_QPA_PLATFORM": "offscreen",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


def _prepare_run(number: int, inputs: PipelineInputs, sandbox: Sandbox) -> PipelineRunState:
    root = sandbox.work / "pipeline" / f"run-{number}"
    (root / "extraction" / "compatible-good" / "proc").mkdir(parents=True)
    (root / "_pca").mkdir()
    (root / "models").mkdir()
    (root / "viz").mkdir()
    shutil.copy2(inputs.pca_components, root / "_pca" / "pca.h5")
    shutil.copy2(inputs.pca_configuration, root / "_pca" / "pca.yaml")
    index = root / "moseq2-index.yaml"
    value = yaml.safe_load(inputs.index.read_text(encoding="utf-8"))
    value["pca_path"] = str(root / "_pca" / "pca_scores_applied.h5")
    for entry in value["files"]:
        filename = entry["filename"]
        entry["path"] = [
            str(inputs.aggregate_results / filename),
            str(inputs.aggregate_results / f"{filename.removesuffix('.h5')}.yaml"),
        ]
    index.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return PipelineRunState(
        number=number,
        root=root,
        index=index,
        commands=[],
        known_failure_results=[],
    )


def _pipeline_failure(failure_id: str) -> KnownFailure:
    return next(item for item in known_failures().failures if item.id == failure_id)


def _exception_from_result(result: dict[str, Any]) -> str | None:
    exception = result.get("exception")
    if isinstance(exception, str) and exception != "SystemExit":
        return exception.split(".")[-1]
    output = "\n".join(
        str(result.get(key, "")) for key in ("message", "traceback", "stdout", "stderr")
    )
    match = re.search(r"([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception))(?::|\b)", output)
    return match.group(1).split(".")[-1] if match else exception


def _step_result(
    step_id: str,
    command: list[str],
    result: dict[str, Any],
    duration: float,
    *,
    expected_failure: str | None = None,
) -> tuple[CommandResult, dict[str, Any] | None]:
    returncode = result.get("returncode")
    returncode = int(returncode) if isinstance(returncode, int) else 1
    if expected_failure is None:
        accepted = returncode == 0
        policy = None
        classification = "passed" if accepted else "failed"
    else:
        expected = _pipeline_failure(expected_failure)
        message = "\n".join(
            str(result.get(key, ""))
            for key in ("message", "traceback", "stdout", "stderr")
            if result.get(key)
        )
        exception = _exception_from_result(result)
        accepted = (
            returncode != 0
            and (expected.signature.exception is None or exception == expected.signature.exception)
            and re.search(expected.signature.message_regex, message, re.DOTALL) is not None
        )
        classification = "known-failure" if accepted else "failed"
        policy = {
            "step": step_id,
            "status": classification if returncode else "unexpected-pass",
            "known_failure_id": expected.id,
            "expected_exception": expected.signature.exception,
            "actual_exception": exception,
            "signature_matched": accepted,
        }
    record = CommandResult(
        id=step_id,
        command=command,
        returncode=returncode,
        duration_seconds=duration,
        stdout=_json(result),
        classification=classification,
    )
    return record, policy


def _execute_seeded(
    state: PipelineRunState,
    *,
    step_id: str,
    module: str,
    arguments: list[str],
    target_python: Path,
    sandbox: Sandbox,
    environment: dict[str, str],
    timeout: int,
    seed: int,
    expected_failure: str | None = None,
) -> None:
    command = [str(target_python), "worker:seeded-cli", module, *arguments]
    started = time.monotonic()
    response = execute_worker(
        target_python,
        WorkerRequest(
            request_id=step_id,
            operation="seeded-cli",
            parameters={
                "module": module,
                "arguments": arguments,
                "seed": seed,
                "cwd": str(state.root),
            },
        ),
        work=sandbox.work / "worker",
        timeout=timeout,
        environment=environment,
    )
    duration = time.monotonic() - started
    if response.result is None:
        raise InvalidConfiguration(f"seeded pipeline step returned no result: {step_id}")
    result, policy = _step_result(
        step_id,
        command,
        response.result,
        duration,
        expected_failure=expected_failure,
    )
    state.commands.append(result)
    if policy is not None:
        state.known_failure_results.append(policy)
    (sandbox.result / f"{step_id}.log").write_text(_json(response.result), encoding="utf-8")


def _execute_command(
    state: PipelineRunState,
    *,
    step_id: str,
    command: list[str],
    target_python: Path,
    sandbox: Sandbox,
    environment: dict[str, str],
    timeout: int,
    expected_failure: str | None = None,
) -> None:
    started = time.monotonic()
    response = execute_worker(
        target_python,
        WorkerRequest(
            request_id=step_id,
            operation="run-command",
            parameters={
                "command": command,
                "cwd": str(state.root),
                "environment": environment,
                "unset_environment": ["DISPLAY", "PYTHONPATH", "PYTHONHOME"],
                "timeout": timeout,
            },
        ),
        work=sandbox.work / "worker",
        timeout=timeout + 30,
    )
    duration = time.monotonic() - started
    if response.result is None:
        raise InvalidConfiguration(f"pipeline command returned no result: {step_id}")
    result, policy = _step_result(
        step_id,
        command,
        response.result,
        duration,
        expected_failure=expected_failure,
    )
    state.commands.append(result)
    if policy is not None:
        state.known_failure_results.append(policy)
    (sandbox.result / f"{step_id}.log").write_text(
        str(response.result.get("stdout", "")) + str(response.result.get("stderr", "")),
        encoding="utf-8",
    )


def execute_extraction_pca_run(
    number: int,
    inputs: PipelineInputs,
    *,
    target_python: Path,
    sandbox: Sandbox,
    timeout: int,
    seed: int,
) -> PipelineRunState:
    """Execute one extraction/PCA half using explicit classifier and local Dask."""
    state = _prepare_run(number, inputs, sandbox)
    environment = _pipeline_environment(target_python, sandbox, seed)
    step_timeout = min(timeout, 600)
    for label, source, failure in (
        (
            "good",
            inputs.raw_good,
            "pipeline-pristine-good-timestamp-mismatch",
        ),
        (
            "bad",
            inputs.raw_bad,
            "pipeline-pristine-bad-timestamp-mismatch",
        ),
    ):
        output = state.root / "extraction" / f"pristine-{label}-failure" / "proc"
        output.mkdir(parents=True)
        _execute_seeded(
            state,
            step_id=f"run-{number}-pristine-{label}-session-timestamp-qc",
            module="moseq2_extract.cli",
            arguments=[
                "extract",
                str(source / "depth.dat"),
                "--output-dir",
                str(output),
                "--flip-classifier",
                str(inputs.classifier),
                "--write-movie",
                "False",
                "--compress",
                "False",
            ],
            target_python=target_python,
            sandbox=sandbox,
            environment=environment,
            timeout=step_timeout,
            seed=seed,
            expected_failure=failure,
        )
    compatible_depth = inputs.compatible_good / "depth.dat"
    extraction_output = state.root / "extraction" / "compatible-good" / "proc"
    _execute_seeded(
        state,
        step_id=f"run-{number}-compatible-good-find-roi",
        module="moseq2_extract.cli",
        arguments=[
            "find-roi",
            str(compatible_depth),
            "--output-dir",
            str(extraction_output),
            "--bg-roi-depth-range",
            "650",
            "750",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
        seed=seed,
    )
    _execute_seeded(
        state,
        step_id=f"run-{number}-compatible-good-extract",
        module="moseq2_extract.cli",
        arguments=[
            "extract",
            str(compatible_depth),
            "--output-dir",
            str(extraction_output),
            "--flip-classifier",
            str(inputs.classifier),
            "--max-height",
            "100",
            "--min-height",
            "10",
            "--chunk-size",
            "1000",
            "--write-movie",
            "True",
            "--compress",
            "False",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
        seed=seed,
    )
    pca_scores = state.root / "_pca" / "pca_scores_applied.h5"
    _execute_command(
        state,
        step_id=f"run-{number}-pca-apply",
        command=[
            str(target_python.parent / "moseq2-pca"),
            "apply-pca",
            "--input-dir",
            str(inputs.aggregate_results),
            "--output-dir",
            str(state.root / "_pca"),
            "--pca-file",
            str(inputs.pca_components),
            "--output-file",
            "pca_scores_applied",
            "--cluster-type",
            "nodask",
            "--chunk-size",
            "1000",
            "--overwrite-pca-apply",
            "True",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    if number == 1:
        _execute_seeded(
            state,
            step_id="run-1-changepoints-nodask-advertised-failure",
            module="moseq2_pca.cli",
            arguments=[
                "compute-changepoints",
                "--input-dir",
                str(inputs.aggregate_results),
                "--output-dir",
                str(state.root / "_pca"),
                "--output-file",
                "changepoints_nodask_failure",
                "--pca-file-components",
                str(state.root / "_pca" / "pca.h5"),
                "--pca-file-scores",
                str(pca_scores),
                "--cluster-type",
                "nodask",
                "--dims",
                "50",
            ],
            target_python=target_python,
            sandbox=sandbox,
            environment=environment,
            timeout=step_timeout,
            seed=seed,
            expected_failure="pipeline-changepoints-nodask-not-implemented",
        )
    _execute_seeded(
        state,
        step_id=f"run-{number}-changepoints-local",
        module="moseq2_pca.cli",
        arguments=[
            "compute-changepoints",
            "--input-dir",
            str(inputs.aggregate_results),
            "--output-dir",
            str(state.root / "_pca"),
            "--output-file",
            "changepoints",
            "--pca-file-components",
            str(state.root / "_pca" / "pca.h5"),
            "--pca-file-scores",
            str(pca_scores),
            "--cluster-type",
            "local",
            "--nworkers",
            "1",
            "--cores",
            "1",
            "--processes",
            "1",
            "--dask-port",
            "0",
            "--dims",
            "50",
            "--chunk-size",
            "1000",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
        seed=seed,
    )
    return state


def _worker_json(
    state: PipelineRunState,
    *,
    request_id: str,
    operation: str,
    parameters: dict[str, Any],
    target_python: Path,
    sandbox: Sandbox,
    environment: dict[str, str],
    timeout: int,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    response = execute_worker(
        target_python,
        WorkerRequest(
            request_id=request_id,
            operation=operation,
            parameters=parameters,
        ),
        work=sandbox.work / "worker",
        timeout=timeout,
        environment=environment,
    )
    duration = time.monotonic() - started
    if response.result is None:
        raise InvalidConfiguration(f"pipeline worker operation returned no result: {request_id}")
    output = sandbox.result / f"{request_id}.json"
    output.write_text(_json(response.result), encoding="utf-8")
    return response.result, duration


def _write_model_analysis(
    state: PipelineRunState,
    *,
    analysis_id: str,
    model_path: Path,
    score_path: Path | None,
    target_python: Path,
    sandbox: Sandbox,
    environment: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {"trusted": True, "path": str(model_path)}
    if score_path is not None:
        parameters["score_path"] = str(score_path)
    result, _ = _worker_json(
        state,
        request_id=analysis_id,
        operation="model-analysis",
        parameters=parameters,
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=timeout,
    )
    summary_path = model_path.with_suffix(".summary.json")
    summary_path.write_text(_json(result["summary"]), encoding="utf-8")
    state.model_summaries[analysis_id] = summary_path
    return result


def execute_downstream_run(
    state: PipelineRunState,
    inputs: PipelineInputs,
    *,
    target_python: Path,
    sandbox: Sandbox,
    timeout: int,
    seed: int,
    reference_model: Path | None = None,
) -> Path:
    """Execute model, visualization, and noninteractive app stages for one run."""
    environment = _pipeline_environment(target_python, sandbox, seed)
    step_timeout = min(timeout, 600)
    pca_scores = state.root / "_pca" / "pca_scores_applied.h5"
    _execute_command(
        state,
        step_id=f"run-{state.number}-legacy-saved-model-apply",
        command=[
            str(target_python.parent / "moseq2-model"),
            "apply-model",
            str(inputs.legacy_model),
            str(pca_scores),
            str(state.root / "models" / "legacy_model_apply_failure.p"),
            "--index",
            str(state.index),
            "--load-groups",
            "True",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
        expected_failure="pipeline-legacy-model-lacks-whitening",
    )

    trained_model = state.root / "models" / "trained_smoke.p"
    _execute_seeded(
        state,
        step_id=f"run-{state.number}-model-train-smoke",
        module="moseq2_model.cli",
        arguments=[
            "learn-model",
            str(pca_scores),
            str(trained_model),
            "--index",
            str(state.index),
            "--num-iter",
            "2",
            "--max-states",
            "10",
            "--npcs",
            "5",
            "--kappa",
            "10000",
            "--ncpus",
            "1",
            "--progressbar",
            "False",
            "--save-every",
            "-1",
            "--save-model",
            "True",
            "--whiten",
            "all",
            "--hold-out-seed",
            "0",
            "--e-step",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
        seed=seed,
    )
    training = _write_model_analysis(
        state,
        analysis_id=f"run-{state.number}-fresh-training-analysis",
        model_path=trained_model,
        score_path=pca_scores,
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    state.training_invariants = dict(training.get("invariants", {}))
    if state.training_invariants.get("passed") is not True:
        raise InvalidConfiguration(
            f"fresh model training invariants failed in run {state.number}: "
            f"{state.training_invariants}"
        )

    selected_reference = reference_model or trained_model
    reference_applied = state.root / "models" / "reference_model_applied.p"
    _execute_command(
        state,
        step_id=f"run-{state.number}-reference-model-apply",
        command=[
            str(target_python.parent / "moseq2-model"),
            "apply-model",
            str(selected_reference),
            str(pca_scores),
            str(reference_applied),
            "--index",
            str(state.index),
            "--load-groups",
            "True",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    reference = _write_model_analysis(
        state,
        analysis_id=f"run-{state.number}-reference-model-analysis",
        model_path=reference_applied,
        score_path=None,
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    specific_syllable = reference.get("specific_syllable")
    if not isinstance(specific_syllable, int) or specific_syllable < 0:
        raise InvalidConfiguration(
            f"reference model has no nonnegative syllable for run {state.number}"
        )
    state.specific_syllable = specific_syllable

    dataframe = state.root / "viz" / "moseq_df.csv"
    _execute_command(
        state,
        step_id=f"run-{state.number}-viz-make-dataframe",
        command=[
            str(target_python.parent / "moseq2-viz"),
            "make-df",
            str(reference_applied),
            str(state.index),
            "--output-file",
            str(dataframe),
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    _execute_command(
        state,
        step_id=f"run-{state.number}-viz-transition-graph",
        command=[
            str(target_python.parent / "moseq2-viz"),
            "plot-transition-graph",
            str(state.index),
            str(reference_applied),
            "--max-syllable",
            "5",
            "--output-file",
            str(state.root / "viz" / "transitions"),
            "--layout",
            "spring",
            "--sort",
            "True",
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    crowd_directory = state.root / "viz" / "crowd_movies"
    _execute_command(
        state,
        step_id=f"run-{state.number}-viz-crowd-movie",
        command=[
            str(target_python.parent / "moseq2-viz"),
            "make-crowd-movies",
            str(state.index),
            str(reference_applied),
            "--specific-syllable",
            str(specific_syllable),
            "--max-examples",
            "3",
            "--processes",
            "1",
            "--output-dir",
            str(crowd_directory),
            "--pad",
            "5",
            "--seed",
            str(seed),
        ],
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )

    app_step = f"run-{state.number}-app-noninteractive-smoke"
    app_result, app_duration = _worker_json(
        state,
        request_id=app_step,
        operation="app-smoke",
        parameters={"index": str(state.index), "model": str(reference_applied)},
        target_python=target_python,
        sandbox=sandbox,
        environment=environment,
        timeout=step_timeout,
    )
    app_output = state.root / "app_smoke.json"
    app_output.write_text(_json(app_result), encoding="utf-8")
    app_passed = app_result.get("status") == "pass"
    state.commands.append(
        CommandResult(
            id=app_step,
            command=[
                str(target_python),
                "worker:app-smoke",
                str(state.index),
                str(reference_applied),
            ],
            returncode=0 if app_passed else 1,
            duration_seconds=app_duration,
            stdout=_json(app_result),
            classification="passed" if app_passed else "failed",
        )
    )
    return trained_model


def _first_crowd_movie(root: Path) -> Path:
    movies = sorted((root / "viz" / "crowd_movies").glob("*.mp4"))
    if len(movies) != 1:
        raise InvalidConfiguration(
            f"expected exactly one crowd movie in {root}, found {len(movies)}"
        )
    return movies[0]


def _pipeline_comparisons(
    run_1: PipelineRunState,
    run_2: PipelineRunState,
    *,
    include_downstream: bool,
    intentional_change: Path | None,
) -> tuple[list[ComparisonResult], list[dict[str, Any]]]:
    specifications: list[tuple[str, Path, Path, str, str, bool]] = [
        (
            "extraction-h5",
            run_1.root / "extraction" / "compatible-good" / "proc" / "results_00.h5",
            run_2.root / "extraction" / "compatible-good" / "proc" / "results_00.h5",
            "extraction-h5",
            "extraction-v1",
            True,
        ),
        (
            "extraction-yaml",
            run_1.root / "extraction" / "compatible-good" / "proc" / "results_00.yaml",
            run_2.root / "extraction" / "compatible-good" / "proc" / "results_00.yaml",
            "yaml",
            "extraction-yaml-v1",
            True,
        ),
        (
            "extraction-video",
            run_1.root / "extraction" / "compatible-good" / "proc" / "results_00.mp4",
            run_2.root / "extraction" / "compatible-good" / "proc" / "results_00.mp4",
            "video",
            "video-v1",
            True,
        ),
        (
            "pca-scores",
            run_1.root / "_pca" / "pca_scores_applied.h5",
            run_2.root / "_pca" / "pca_scores_applied.h5",
            "hdf5",
            "hdf5-v1",
            True,
        ),
        (
            "changepoints",
            run_1.root / "_pca" / "changepoints.h5",
            run_2.root / "_pca" / "changepoints.h5",
            "hdf5",
            "hdf5-v1",
            True,
        ),
    ]
    if include_downstream:
        specifications.extend(
            [
                (
                    "reference-model-application",
                    run_1.model_summaries["run-1-reference-model-analysis"],
                    run_2.model_summaries["run-2-reference-model-analysis"],
                    "trusted-model",
                    "model-v1",
                    True,
                ),
                (
                    "viz-dataframe",
                    run_1.root / "viz" / "moseq_df.csv",
                    run_2.root / "viz" / "moseq_df.csv",
                    "table",
                    "table-v1",
                    True,
                ),
                (
                    "crowd-movie",
                    _first_crowd_movie(run_1.root),
                    _first_crowd_movie(run_2.root),
                    "video",
                    "video-v1",
                    True,
                ),
                (
                    "app-smoke",
                    run_1.root / "app_smoke.json",
                    run_2.root / "app_smoke.json",
                    "yaml",
                    "yaml-v1",
                    True,
                ),
                (
                    "fresh-training-structure",
                    run_1.model_summaries["run-1-fresh-training-analysis"],
                    run_2.model_summaries["run-2-fresh-training-analysis"],
                    "trusted-model",
                    "model-v1",
                    False,
                ),
            ]
        )
    change = load_intentional_change(intentional_change) if intentional_change else None
    results: list[ComparisonResult] = []
    index: list[dict[str, Any]] = []
    for comparison_id, expected, actual, kind, policy, required_equal in specifications:
        if not expected.is_file() or not actual.is_file():
            raise InvalidConfiguration(f"missing pipeline comparison artifact: {comparison_id}")
        result = compare(
            kind,
            expected,
            actual,
            policy,
            intentional_change=change,
        )
        results.append(result)
        index.append(
            {
                "id": comparison_id,
                "required_equal": required_equal,
                "expected": (
                    Path("outputs") / "run-1" / expected.relative_to(run_1.root)
                ).as_posix(),
                "actual": (Path("outputs") / "run-2" / actual.relative_to(run_2.root)).as_posix(),
                "result_index": len(results) - 1,
                "status": result.status.value,
            }
        )
    return results, index


def _pipeline_run_id(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _copy_pipeline_evidence(
    sandbox: Sandbox,
    run_directory: Path,
    states: list[PipelineRunState],
) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for state in states:
        destination = run_directory / "outputs" / f"run-{state.number}"
        shutil.copytree(state.root, destination)
        retained.append(
            {
                "id": f"run-{state.number}",
                "path": destination.relative_to(run_directory).as_posix(),
                "files": len(snapshot_tree(destination)),
            }
        )
    logs = run_directory / "logs"
    for root in (sandbox.result, sandbox.work / "worker"):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if path.is_file():
                destination = logs / path.name
                if destination.exists():
                    destination = logs / f"{root.name}-{path.name}"
                shutil.copy2(path, destination)
    return retained


def _validate_pipeline_selection(options: Any) -> bool:
    if options.packages or options.steps:
        raise InvalidConfiguration(
            "pipeline package/step diagnostic selection will be enabled with package integrations"
        )
    allowed_stages = {"prepare", "extract", "pca", "model", "viz", "app", "compare"}
    if options.start_at not in allowed_stages | {None}:
        raise InvalidConfiguration(f"unknown pipeline --start-at stage: {options.start_at}")
    if options.through not in allowed_stages | {None}:
        raise InvalidConfiguration(f"unknown pipeline --through stage: {options.through}")
    if options.start_at not in {None, "model"}:
        raise InvalidConfiguration("only --start-at model is currently meaningful for this DAG")
    if options.through not in {None, "pca"}:
        raise InvalidConfiguration("only --through pca is currently supported for this DAG")
    if options.start_at == "model" and options.through == "pca":
        raise InvalidConfiguration("--start-at model cannot be combined with --through pca")
    return bool(options.through != "pca")


def _pipeline_target(
    base_python: Path,
    sandbox: Sandbox,
    resolved_candidates: list[CandidateRecord],
    candidate_records: list[CandidateRecord],
    *,
    wheel_root: Path,
    timeout: int,
) -> Path:
    """Create a full-stack target only when at least one candidate replaces a lock."""
    if not candidate_records:
        return base_python
    installation_records = _install_records(resolved_candidates, wheel_root=wheel_root)
    return _create_layered_target(
        base_python, sandbox, installation_records, timeout, force=True
    )


def run_pipeline_smoke(options: Any, profile: SuiteProfile) -> tuple[Path, int]:
    """Run the installed eight-package compact real-data pipeline twice."""
    started_at = datetime.now(UTC)
    run_id = _pipeline_run_id(started_at)
    run_directory = (options.output_dir / run_id).resolve()
    include_downstream = _validate_pipeline_selection(options)
    timeout = options.timeout or profile.resources.timeout_seconds
    if timeout > profile.resources.timeout_seconds:
        raise InvalidConfiguration("--timeout cannot exceed the profile certification ceiling")
    if options.keep_sandbox not in {"always", "failure", "never"}:
        raise InvalidConfiguration("--keep-sandbox must be always, failure, or never")
    if options.executor != "process":
        raise InvalidConfiguration("pipeline container execution is unavailable until P10")
    if options.target_python is None:
        raise MissingInput("pipeline process execution requires --target-python")
    if options.fixture_set not in {None, "pipeline-smoke-v1"}:
        raise InvalidConfiguration("pipeline-smoke requires fixture set pipeline-smoke-v1")
    base_python = options.target_python.expanduser().absolute()
    if not base_python.is_file():
        raise MissingInput(f"target Python does not exist: {base_python}")

    sandbox = Sandbox.create(options.workspace, prefix="moseq2-test-pipeline-")
    source_manifest = source_lock(options.baseline_lock)
    wheel_manifest = wheel_lock("moseq2-baseline-linux-py37-v1")
    fixture = fixture_manifest("pipeline-smoke-v1")
    commands: list[CommandResult] = []
    comparisons: list[ComparisonResult] = []
    comparison_index: list[dict[str, Any]] = []
    policy_results: list[dict[str, Any]] = []
    resolved_candidates: list[CandidateRecord] = []
    states: list[PipelineRunState] = []
    target_python = base_python
    inputs: PipelineInputs | None = None
    failure_stage: str | None = None
    environment: dict[str, Any] = {"controller": controller_environment()}
    provenance: dict[str, Any] = {
        "environment_variables": redacted_environment(),
        "executor": "process",
        "dask": {"mode": "local", "workers": 1, "slurm_submitted": False},
        "requested_slice": {"start_at": options.start_at, "through": options.through},
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
            raise InvalidConfiguration("duplicate pipeline candidate packages")
        candidate_by_name = {canonical_package(item.package): item for item in candidate_records}
        unknown = set(candidate_by_name) - {
            canonical_package(item.package) for item in baseline_candidates
        }
        if unknown:
            raise InvalidConfiguration(f"candidate set has unknown packages: {sorted(unknown)}")
        resolved_candidates = [
            candidate_by_name.get(canonical_package(item.package), item)
            for item in baseline_candidates
        ]
        resolved_candidates = _apply_test_sources(resolved_candidates, options.test_sources)
        # The inherited system site-packages expose baseline imports, but their
        # console scripts live in the base environment's bin directory.  A
        # candidate target must therefore install the complete resolved stack,
        # not only the changed wheel, because the compact pipeline invokes the
        # extract/PCA/model/viz/app entry points from the target environment.
        target_python = _pipeline_target(
            base_python,
            sandbox,
            resolved_candidates,
            candidate_records,
            wheel_root=roots["wheel"],
            timeout=timeout,
        )

        mirror_value = os.environ.get("MOSEQ2_TEST_FIXTURE_MIRROR")
        mirror = Path(mirror_value) if mirror_value else None
        preparation_started = time.monotonic()
        inputs = derive_pipeline_inputs(
            options.cache_dir,
            sandbox.inputs / "pipeline-project",
            mirror=mirror,
            offline=options.offline,
        )
        commands.append(
            CommandResult(
                id="prepare-compact-real-data-project",
                command=["moseq2-test", "data", "derive", "pipeline-smoke-v1"],
                returncode=0,
                duration_seconds=time.monotonic() - preparation_started,
                stdout=_json(inputs.derivation),
                classification="passed",
            )
        )
        input_snapshot = snapshot_tree(inputs.root)
        run_1 = execute_extraction_pca_run(
            1,
            inputs,
            target_python=target_python,
            sandbox=sandbox,
            timeout=timeout,
            seed=options.seed,
        )
        states.append(run_1)
        if include_downstream:
            reference = execute_downstream_run(
                run_1,
                inputs,
                target_python=target_python,
                sandbox=sandbox,
                timeout=timeout,
                seed=options.seed,
            )
        else:
            reference = None
        run_2 = execute_extraction_pca_run(
            2,
            inputs,
            target_python=target_python,
            sandbox=sandbox,
            timeout=timeout,
            seed=options.seed,
        )
        states.append(run_2)
        if include_downstream:
            execute_downstream_run(
                run_2,
                inputs,
                target_python=target_python,
                sandbox=sandbox,
                timeout=timeout,
                seed=options.seed,
                reference_model=reference,
            )
        assert_unchanged(inputs.root, input_snapshot)
        commands.extend(run_1.commands)
        commands.extend(run_2.commands)
        policy_results = run_1.known_failure_results + run_2.known_failure_results
        comparisons, comparison_index = _pipeline_comparisons(
            run_1,
            run_2,
            include_downstream=include_downstream,
            intentional_change=options.intentional_change,
        )
        environment["target"] = {
            "python": str(target_python),
            "base_python": str(base_python),
        }
        environment["pipeline_results"] = {
            "full_profile": include_downstream,
            "input_derivation": inputs.derivation,
            "runs": {
                f"run-{state.number}": {
                    "training_invariants": state.training_invariants,
                    "specific_syllable": state.specific_syllable,
                }
                for state in states
            },
            "comparison_index": comparison_index,
        }
        provenance["display_paths"] = {
            "sandbox": str(sandbox.root),
            "base_python": str(base_python),
            "target_python": str(target_python),
            "fixture_mirror": str(mirror) if mirror else None,
        }
        provenance["flip_classifier"] = inputs.derivation["flip_classifier"]
    except Moseq2TestError as error:
        failure_stage = "setup"
        commands.append(
            CommandResult(
                id="setup",
                command=["moseq2-test", "run", "pipeline-smoke"],
                returncode=int(error.exit_code),
                duration_seconds=0,
                stdout=_json({"error_type": type(error).__name__, "error": str(error)}),
                classification="failed",
            )
        )
    except Exception as error:
        failure_stage = "infrastructure"
        commands.append(
            CommandResult(
                id="infrastructure",
                command=["moseq2-test", "run", "pipeline-smoke"],
                returncode=1,
                duration_seconds=0,
                stdout=_json({"error_type": type(error).__name__, "error": str(error)}),
                classification="failed",
            )
        )

    accepted_classes = {"passed", "known-failure"}
    required_comparisons = [
        comparisons[item["result_index"]] for item in comparison_index if item["required_equal"]
    ]
    comparison_pass = all(
        result.status in {ComparisonStatus.EQUAL, ComparisonStatus.EXPECTED_CHANGE}
        for result in required_comparisons
    )
    comparison_valid = all(
        result.status not in {ComparisonStatus.INVALID, ComparisonStatus.ERROR}
        for result in comparisons
    )
    expected_command_count = 28 if include_downstream else 14
    expected_comparison_count = 10 if include_downstream else 5
    expected_required_count = 9 if include_downstream else 5
    training_pass = not include_downstream or all(
        state.training_invariants.get("passed") is True for state in states
    )
    expected_ids = [
        step.id
        for step in profile.steps
        if include_downstream or step.stage in {"prepare", "extract", "pca"}
    ]
    expected_failure_counts = {
        "pipeline-pristine-good-timestamp-mismatch": 2,
        "pipeline-pristine-bad-timestamp-mismatch": 2,
        "pipeline-changepoints-nodask-not-implemented": 1,
    }
    if include_downstream:
        expected_failure_counts["pipeline-legacy-model-lacks-whitening"] = 2
    actual_failure_counts = {
        failure_id: sum(
            value.get("known_failure_id") == failure_id and value.get("status") == "known-failure"
            for value in policy_results
        )
        for failure_id in expected_failure_counts
    }
    failure_policy_pass = actual_failure_counts == expected_failure_counts and len(
        policy_results
    ) == sum(expected_failure_counts.values())
    environment.setdefault("pipeline_results", {}).update(
        {
            "expected_command_ids": expected_ids,
            "known_failure_counts": actual_failure_counts,
            "expected_known_failure_counts": expected_failure_counts,
        }
    )
    accepted = (
        failure_stage is None
        and len(commands) == expected_command_count
        and [command.id for command in commands] == expected_ids
        and all(command.classification in accepted_classes for command in commands)
        and len(comparisons) == expected_comparison_count
        and len(required_comparisons) == expected_required_count
        and comparison_pass
        and comparison_valid
        and training_pass
        and failure_policy_pass
    )
    if not accepted and failure_stage is None:
        failure_stage = "pipeline"
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
        fixture_sets=[fixture.fixture_set],
        candidates=resolved_candidates,
        commands=commands,
        comparisons=comparisons,
        known_failure_results=policy_results,
        environment=environment,
        provenance=provenance,
    )
    write_run_directory(
        run_directory,
        record,
        resolved_config={
            "profile": profile.name,
            "baseline_lock": options.baseline_lock,
            "fixture_set": fixture.fixture_set,
            "seed": options.seed,
            "start_at": options.start_at,
            "through": options.through,
        },
    )
    (run_directory / "source-lock.json").write_text(
        _json(source_manifest.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "wheel-lock.json").write_text(
        _json(wheel_manifest.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "fixture-manifest.json").write_text(
        _json(fixture.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "manifests" / "suite-profile.json").write_text(
        _json(profile.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "comparisons" / "index.json").write_text(
        _json(comparison_index), encoding="utf-8"
    )
    retained = _copy_pipeline_evidence(sandbox, run_directory, states)
    if retained:
        record = record.model_copy(update={"retained_outputs": retained})
        write_run_directory(
            run_directory,
            record,
            resolved_config={
                "profile": profile.name,
                "baseline_lock": options.baseline_lock,
                "fixture_set": fixture.fixture_set,
                "seed": options.seed,
                "start_at": options.start_at,
                "through": options.through,
            },
        )
        (run_directory / "comparisons" / "index.json").write_text(
            _json(comparison_index), encoding="utf-8"
        )
    retain_sandbox = options.keep_sandbox == "always" or (
        options.keep_sandbox == "failure" and not accepted
    )
    if retain_sandbox:
        (run_directory / "sandbox.txt").write_text(str(sandbox.root) + "\n", encoding="utf-8")
    else:
        sandbox.cleanup()
    return run_directory, int(ExitCode.ACCEPTED if accepted else ExitCode.DIFFERENCE)
