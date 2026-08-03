"""Parsed YAML comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from moseq2_test.compare.base import ComparatorPolicy, jsonable, remove_ignored


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return jsonable(yaml.safe_load(stream))


def manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    return {
        "kind": "yaml",
        "canonical_value": remove_ignored(_load(path), "", policy.ignore_patterns),
    }


def compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    left = manifest(expected, policy)["canonical_value"]
    right = manifest(actual, policy)["canonical_value"]
    return [] if left == right else [{"kind": "canonical_value", "expected": left, "actual": right}]
