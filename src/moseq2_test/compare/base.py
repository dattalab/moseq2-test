"""Shared semantic comparison contracts and policy loading."""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from moseq2_test.config import load_yaml, resource
from moseq2_test.errors import InvalidConfiguration


class ComparatorPolicy(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: int = 1
    name: str
    kind: str
    rtol: float = Field(default=0.0, ge=0)
    atol: float = Field(default=0.0, ge=0)
    ignore: list[str] = Field(default_factory=list)
    compare_attributes: bool = True
    require_dtype: bool = True
    require_order: bool = True

    @property
    def ignore_patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(value) for value in self.ignore]


def load_policy(name: str) -> ComparatorPolicy:
    filename = name if name.endswith(".yml") else f"{name}.yml"
    with resource("manifests", "comparator-policies", filename) as path:
        if not path.is_file():
            raise InvalidConfiguration(f"unknown comparator policy: {name}")
        return ComparatorPolicy.model_validate(load_yaml(path))


def ignored(path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(path) for pattern in patterns)


def jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return {"bytes_utf8": value.decode("utf-8")}
        except UnicodeDecodeError:
            return {"bytes_hex": value.hex()}
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): jsonable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"float": "nan"}
        if math.isinf(value):
            return {"float": "inf" if value > 0 else "-inf"}
        return value
    return {"python_type": type(value).__module__ + "." + type(value).__name__}


def remove_ignored(value: Any, prefix: str, patterns: list[re.Pattern[str]]) -> Any:
    if ignored(prefix or "/", patterns):
        return {"ignored": True}
    if isinstance(value, dict):
        return {
            key: remove_ignored(item, f"{prefix}/{key}" if prefix else f"/{key}", patterns)
            for key, item in value.items()
            if not ignored(f"{prefix}/{key}" if prefix else f"/{key}", patterns)
        }
    if isinstance(value, list):
        return [
            remove_ignored(item, f"{prefix}/{index}", patterns) for index, item in enumerate(value)
        ]
    return value


def arrays_differences(
    expected: np.ndarray,
    actual: np.ndarray,
    *,
    path: str,
    policy: ComparatorPolicy,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if expected.shape != actual.shape:
        return [
            {
                "kind": "shape",
                "path": path,
                "expected": list(expected.shape),
                "actual": list(actual.shape),
            }
        ]
    if policy.require_dtype and expected.dtype != actual.dtype:
        differences.append(
            {
                "kind": "dtype",
                "path": path,
                "expected": str(expected.dtype),
                "actual": str(actual.dtype),
            }
        )
        return differences
    if np.issubdtype(expected.dtype, np.floating) or np.issubdtype(
        expected.dtype, np.complexfloating
    ):
        close = np.isclose(
            expected,
            actual,
            rtol=policy.rtol,
            atol=policy.atol,
            equal_nan=True,
        )
    else:
        close = np.asarray(expected == actual)
    if not bool(np.all(close)):
        finite = np.isfinite(expected) & np.isfinite(actual)
        maximum = (
            float(np.max(np.abs(expected[finite] - actual[finite])))
            if np.any(finite) and np.issubdtype(expected.dtype, np.number)
            else None
        )
        differences.append(
            {
                "kind": "values",
                "path": path,
                "mismatches": int(np.size(close) - np.count_nonzero(close)),
                "maximum_absolute_difference": maximum,
                "rtol": policy.rtol,
                "atol": policy.atol,
            }
        )
    return differences
