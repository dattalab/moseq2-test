"""PCA comparison with component-sign alignment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py  # type: ignore[import-untyped]
import numpy as np

from moseq2_test.compare.base import ComparatorPolicy, arrays_differences


def _read(path: Path, dataset: str) -> np.ndarray:
    if path.suffix.lower() == ".npz":
        with np.load(path, allow_pickle=False) as archive:
            return np.asarray(archive[dataset.lstrip("/")])
    with h5py.File(path, "r") as handle:
        return np.asarray(handle[dataset][...])


def manifest(path: Path, policy: ComparatorPolicy) -> dict[str, Any]:
    components_path = str((policy.model_extra or {}).get("components_path", "/components"))
    components = _read(path, components_path)
    return {
        "kind": "pca",
        "components_path": components_path,
        "components_shape": list(components.shape),
        "sign_alignment": True,
    }


def compare(expected: Path, actual: Path, policy: ComparatorPolicy) -> list[dict[str, Any]]:
    extra = policy.model_extra or {}
    components_path = str(extra.get("components_path", "/components"))
    scores_path = extra.get("scores_path", "/scores")
    variance_path = extra.get("variance_path", "/explained_variance")
    left_components = _read(expected, components_path)
    right_components = _read(actual, components_path)
    if left_components.shape != right_components.shape:
        return arrays_differences(
            left_components, right_components, path=components_path, policy=policy
        )
    signs = np.ones(left_components.shape[0], dtype=float)
    for index in range(left_components.shape[0]):
        if np.vdot(left_components[index].ravel(), right_components[index].ravel()).real < 0:
            signs[index] = -1.0
    aligned_components = right_components * signs[:, None]
    differences = arrays_differences(
        left_components, aligned_components, path=components_path, policy=policy
    )
    if scores_path:
        left_scores = _read(expected, str(scores_path))
        right_scores = _read(actual, str(scores_path))
        aligned_scores = right_scores * signs[: right_scores.shape[1]][None, :]
        differences.extend(
            arrays_differences(left_scores, aligned_scores, path=str(scores_path), policy=policy)
        )
    if variance_path:
        differences.extend(
            arrays_differences(
                _read(expected, str(variance_path)),
                _read(actual, str(variance_path)),
                path=str(variance_path),
                policy=policy,
            )
        )
    if not differences:
        gram_expected = left_components @ left_components.T
        gram_actual = aligned_components @ aligned_components.T
        differences.extend(
            arrays_differences(
                gram_expected,
                gram_actual,
                path="/component-subspace-gram",
                policy=policy,
            )
        )
    return differences
