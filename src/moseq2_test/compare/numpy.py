"""NumPy array and archive comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from moseq2_test.compare.base import ComparatorPolicy, arrays_differences


def _load(path: Path) -> dict[str, np.ndarray]:
    value = np.load(path, allow_pickle=False)
    if isinstance(value, np.lib.npyio.NpzFile):
        try:
            return {name: np.asarray(value[name]) for name in value.files}
        finally:
            value.close()
    return {"array": np.asarray(value)}


def manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    del policy
    return {
        "kind": "numpy",
        "arrays": {
            name: {"shape": list(value.shape), "dtype": str(value.dtype)}
            for name, value in _load(path).items()
        },
    }


def compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    left = _load(expected)
    right = _load(actual)
    if set(left) != set(right):
        return [{"kind": "arrays", "expected": sorted(left), "actual": sorted(right)}]
    differences: list[dict[str, Any]] = []
    for name in sorted(left):
        differences.extend(
            arrays_differences(left[name], right[name], path=f"/{name}", policy=policy)
        )
    return differences
