"""Trusted-model summary comparison without controller-side unpickling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moseq2_test.compare.base import ComparatorPolicy, remove_ignored
from moseq2_test.errors import InvalidConfiguration


def _load(path: Path, policy: ComparatorPolicy) -> Any:
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        raise InvalidConfiguration(
            "trusted-model comparison accepts worker-generated JSON/YAML "
            "summaries, not pickle bytes"
        )
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        import yaml

        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return remove_ignored(value, "", policy.ignore_patterns)


def manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    return {"kind": "trusted-model-summary", "structure": _load(path, policy)}


def compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    left = _load(expected, policy)
    right = _load(actual, policy)
    return [] if left == right else [{"kind": "model-summary", "expected": left, "actual": right}]
