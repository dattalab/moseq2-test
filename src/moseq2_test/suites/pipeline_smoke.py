"""Compact, real-data MoSeq2 pipeline fixtures and execution."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py  # type: ignore[import-untyped]
import yaml

from moseq2_test.data import CacheLayout, extract_object, fetch_selected
from moseq2_test.errors import InvalidConfiguration
from moseq2_test.execution.process import execute_worker
from moseq2_test.execution.protocol import WorkerRequest
from moseq2_test.models import CommandResult, FixtureManifest, FixtureObject, KnownFailure
from moseq2_test.registry import fixture_manifest, known_failures
from moseq2_test.sandbox import Sandbox
from moseq2_test.suites.install_smoke import _json

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
