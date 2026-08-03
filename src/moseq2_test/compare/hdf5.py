"""Recursive, policy-driven HDF5 comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py  # type: ignore[import-untyped]
import numpy as np

from moseq2_test.compare.base import (
    ComparatorPolicy,
    arrays_differences,
    ignored,
    jsonable,
    remove_ignored,
)


def _paths(handle: h5py.File) -> tuple[set[str], set[str]]:
    groups = {"/"}
    datasets: set[str] = set()

    def visit(name: str, value: Any) -> None:
        path = "/" + name
        if isinstance(value, h5py.Group):
            groups.add(path)
        elif isinstance(value, h5py.Dataset):
            datasets.add(path)

    handle.visititems(visit)
    return groups, datasets


def manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    datasets: dict[str, Any] = {}
    with h5py.File(path, "r") as handle:
        group_paths, dataset_paths = _paths(handle)
        for group_path in sorted(group_paths):
            if ignored(group_path, policy.ignore_patterns):
                continue
            groups[group_path] = {
                "attributes": jsonable(dict(handle[group_path].attrs))
                if policy.compare_attributes
                else None
            }
        for dataset_path in sorted(dataset_paths):
            if ignored(dataset_path, policy.ignore_patterns):
                continue
            dataset = handle[dataset_path]
            datasets[dataset_path] = {
                "shape": list(dataset.shape),
                "dtype": str(dataset.dtype),
                "attributes": jsonable(dict(dataset.attrs)) if policy.compare_attributes else None,
            }
    return {"kind": "hdf5", "groups": groups, "datasets": datasets}


def compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    patterns = policy.ignore_patterns
    with h5py.File(expected, "r") as left, h5py.File(actual, "r") as right:
        left_groups, left_datasets = _paths(left)
        right_groups, right_datasets = _paths(right)
        for kind, left_paths, right_paths in (
            ("group", left_groups, right_groups),
            ("dataset", left_datasets, right_datasets),
        ):
            missing = sorted(
                path for path in left_paths - right_paths if not ignored(path, patterns)
            )
            added = sorted(path for path in right_paths - left_paths if not ignored(path, patterns))
            if missing:
                differences.append({"kind": f"missing_{kind}s", "paths": missing})
            if added:
                differences.append({"kind": f"added_{kind}s", "paths": added})
        shared = (left_groups & right_groups) | (left_datasets & right_datasets)
        if policy.compare_attributes:
            for path in sorted(shared):
                if ignored(path, patterns):
                    continue
                attribute_path = path.rstrip("/") + "/@attributes"
                left_value = remove_ignored(
                    jsonable(dict(left[path].attrs)), attribute_path, patterns
                )
                right_value = remove_ignored(
                    jsonable(dict(right[path].attrs)), attribute_path, patterns
                )
                if left_value != right_value:
                    differences.append(
                        {
                            "kind": "attributes",
                            "path": path,
                            "expected": left_value,
                            "actual": right_value,
                        }
                    )
        for path in sorted(left_datasets & right_datasets):
            if ignored(path, patterns):
                continue
            differences.extend(
                arrays_differences(
                    np.asarray(left[path][...]),
                    np.asarray(right[path][...]),
                    path=path,
                    policy=policy,
                )
            )
    return differences
