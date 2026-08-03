"""Typed comparator registry and status conversion."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml as yaml_parser

from moseq2_test.compare import hdf5, media, model, numpy, pca, tables, yaml
from moseq2_test.compare.base import ComparatorPolicy, load_policy
from moseq2_test.errors import InvalidConfiguration
from moseq2_test.models import (
    ComparisonResult,
    ComparisonStatus,
    IntentionalChange,
    RunRecord,
)
from moseq2_test.provenance import sha256_file

CompareFunction = Callable[[Path, Path, ComparatorPolicy], list[dict[str, Any]]]
ManifestFunction = Callable[[Path, ComparatorPolicy], dict[str, Any]]


def _binary_manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    del policy
    return {"kind": "binary", "size": path.stat().st_size, "sha256": sha256_file(path)}


def _binary_compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    del policy
    left = sha256_file(expected)
    right = sha256_file(actual)
    return [] if left == right else [{"kind": "file-sha256", "expected": left, "actual": right}]


REGISTRY: dict[str, tuple[ManifestFunction, CompareFunction]] = {
    "binary": (_binary_manifest, _binary_compare),
    "extraction-h5": (hdf5.manifest, hdf5.compare),
    "hdf5": (hdf5.manifest, hdf5.compare),
    "yaml": (yaml.manifest, yaml.compare),
    "config-yaml": (yaml.manifest, yaml.compare),
    "table": (tables.manifest, tables.compare),
    "numpy": (numpy.manifest, numpy.compare),
    "pca": (pca.manifest, pca.compare),
    "trusted-model": (model.manifest, model.compare),
    "image": (media.image_manifest, media.compare_image),
    "video": (media.video_manifest, media.compare_video),
}


def comparator_names() -> list[str]:
    return sorted(REGISTRY)


def artifact_manifest(kind: str, path: Path, policy_name: str) -> dict[str, Any]:
    if kind not in REGISTRY:
        raise InvalidConfiguration(f"unknown comparator kind: {kind}")
    policy = load_policy(policy_name)
    if policy.kind not in {kind, "any"}:
        raise InvalidConfiguration(
            f"policy {policy.name} is for {policy.kind}, not requested kind {kind}"
        )
    return {
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "semantic": REGISTRY[kind][0](path, policy),
        "policy": policy.name,
    }


def _apply_intentional_change(
    result: ComparisonResult, change: IntentionalChange | None
) -> ComparisonResult:
    if result.status != ComparisonStatus.DIFFERENT or change is None:
        return result
    if result.kind not in change.affected_artifacts:
        return result
    return result.model_copy(
        update={
            "status": ComparisonStatus.EXPECTED_CHANGE,
            "summary": f"difference accepted by intentional change {change.id}",
        }
    )


def compare(
    kind: str,
    expected: Path,
    actual: Path,
    policy_name: str,
    *,
    intentional_change: IntentionalChange | None = None,
) -> ComparisonResult:
    expected_hash = sha256_file(expected)
    actual_hash = sha256_file(actual)
    if kind not in REGISTRY:
        return ComparisonResult(
            status=ComparisonStatus.INVALID,
            kind=kind,
            comparator="registry",
            comparator_version="1",
            expected_sha256=expected_hash,
            actual_sha256=actual_hash,
            policy=policy_name,
            summary=f"unknown comparator kind {kind}",
        )
    try:
        policy = load_policy(policy_name)
        if policy.kind not in {kind, "any"}:
            raise InvalidConfiguration(
                f"policy {policy.name} is for {policy.kind}, not requested kind {kind}"
            )
        differences = REGISTRY[kind][1](expected, actual, policy)
        status = ComparisonStatus.EQUAL if not differences else ComparisonStatus.DIFFERENT
        result = ComparisonResult(
            status=status,
            kind=kind,
            comparator=f"moseq2-test:{kind}",
            comparator_version="1",
            expected_sha256=expected_hash,
            actual_sha256=actual_hash,
            policy=policy.name,
            tolerances={"rtol": policy.rtol, "atol": policy.atol},
            ignored_fields=policy.ignore,
            differences=differences,
            summary="semantic values are equal"
            if not differences
            else f"{len(differences)} semantic difference(s)",
        )
        return _apply_intentional_change(result, intentional_change)
    except InvalidConfiguration as error:
        return ComparisonResult(
            status=ComparisonStatus.INVALID,
            kind=kind,
            comparator=f"moseq2-test:{kind}",
            comparator_version="1",
            expected_sha256=expected_hash,
            actual_sha256=actual_hash,
            policy=policy_name,
            summary=str(error),
        )
    except Exception as error:
        return ComparisonResult(
            status=ComparisonStatus.ERROR,
            kind=kind,
            comparator=f"moseq2-test:{kind}",
            comparator_version="1",
            expected_sha256=expected_hash,
            actual_sha256=actual_hash,
            policy=policy_name,
            differences=[{"error_type": type(error).__name__, "error": str(error)}],
            summary=f"comparator failed: {type(error).__name__}: {error}",
        )


def _canonical_run(path: Path) -> dict[str, Any]:
    record = RunRecord.model_validate_json((path / "run.json").read_text(encoding="utf-8"))
    value = record.model_dump(mode="json")
    provenance = value.get("provenance", {})
    display_paths = provenance.get("display_paths", {})
    sandbox_roots: set[str] = set()
    if isinstance(display_paths, dict):
        for display_path in display_paths.values():
            if not isinstance(display_path, str) or not display_path.startswith("/"):
                continue
            candidate = Path(display_path)
            for parent in (candidate, *candidate.parents):
                if parent.name.startswith("moseq2-test-"):
                    sandbox_roots.add(str(parent))
                    break

    def normalize_ephemeral_paths(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: normalize_ephemeral_paths(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [normalize_ephemeral_paths(nested) for nested in item]
        if isinstance(item, str):
            for sandbox_root in sorted(sandbox_roots, key=len, reverse=True):
                item = item.replace(sandbox_root, "<sandbox>")
        return item

    value = cast(dict[str, Any], normalize_ephemeral_paths(value))
    value.pop("run_id", None)
    value.pop("started_at", None)
    value.pop("completed_at", None)
    for command in value.get("commands", []):
        command.pop("duration_seconds", None)
        command.pop("stdout", None)
        command.pop("stderr", None)
    for comparison in value.get("comparisons", []):
        # Byte hashes retain audit value in the individual run record, but they
        # are deliberately non-semantic whenever that artifact comparator has
        # ignored fields or tolerances. Compare the comparator's status and
        # structured differences here instead.
        comparison.pop("expected_sha256", None)
        comparison.pop("actual_sha256", None)
    historical_suites = (
        value.get("environment", {}).get("historical_results", {}).get("suites", {})
    )
    if isinstance(historical_suites, dict):
        for suite in historical_suites.values():
            if isinstance(suite, dict):
                suite.pop("time", None)
    provenance = value.get("provenance", {})
    provenance.pop("display_paths", None)
    environment_variables = provenance.get("environment_variables", {})
    if isinstance(environment_variables, dict):
        environment_variables.pop("HOSTNAME", None)
    return value


def compare_runs(expected_run: Path, actual_run: Path) -> ComparisonResult:
    left = _canonical_run(expected_run)
    right = _canonical_run(actual_run)
    differences = (
        [] if left == right else [{"kind": "canonical-run", "expected": left, "actual": right}]
    )
    return ComparisonResult(
        status=ComparisonStatus.EQUAL if not differences else ComparisonStatus.DIFFERENT,
        kind="run-record",
        comparator="moseq2-test:run-record",
        comparator_version="1",
        expected_sha256=sha256_file(expected_run / "run.json"),
        actual_sha256=sha256_file(actual_run / "run.json"),
        policy="run-record-v1",
        differences=differences,
        summary="canonical run semantics are equal"
        if not differences
        else "canonical run semantics differ",
    )


def load_intentional_change(path: Path) -> IntentionalChange:
    value = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.suffix == ".json"
        else yaml_parser.safe_load(path.read_text(encoding="utf-8"))
    )
    return IntentionalChange.model_validate(value)
