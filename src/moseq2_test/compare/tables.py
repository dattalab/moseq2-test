"""CSV/TSV/Parquet semantic comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from moseq2_test.compare.base import ComparatorPolicy, arrays_differences


def _load(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    separator = "\t" if path.suffix.lower() == ".tsv" else ","
    return pd.read_csv(path, sep=separator)


def manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    frame = _load(path)
    return {
        "kind": "table",
        "rows": len(frame),
        "columns": [
            {"name": str(name), "dtype": str(frame.dtypes.iloc[index])}
            for index, name in enumerate(frame.columns)
        ],
        "order_required": policy.require_order,
    }


def compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    left = _load(expected)
    right = _load(actual)
    differences: list[dict[str, Any]] = []
    if policy.require_order:
        if list(left.columns) != list(right.columns):
            return [
                {
                    "kind": "columns",
                    "expected": list(map(str, left.columns)),
                    "actual": list(map(str, right.columns)),
                }
            ]
    elif set(left.columns) == set(right.columns):
        right = right[list(left.columns)]
    else:
        return [
            {"kind": "columns", "expected": sorted(left.columns), "actual": sorted(right.columns)}
        ]
    if len(left) != len(right):
        return [{"kind": "rows", "expected": len(left), "actual": len(right)}]
    for column in left.columns:
        left_values = left[column].to_numpy()
        right_values = right[column].to_numpy()
        if left_values.dtype.kind in "OUS" or right_values.dtype.kind in "OUS":
            equal = np.asarray(left_values == right_values)
            if not bool(np.all(equal)):
                differences.append(
                    {
                        "kind": "values",
                        "path": f"/{column}",
                        "mismatches": int(np.size(equal) - np.count_nonzero(equal)),
                    }
                )
        else:
            differences.extend(
                arrays_differences(left_values, right_values, path=f"/{column}", policy=policy)
            )
    return differences
