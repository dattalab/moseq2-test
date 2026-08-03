from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
from PIL import Image

from moseq2_test.compare.registry import compare
from moseq2_test.models import ComparisonStatus, IntentionalChange


def write_hdf5(path: Path, values: np.ndarray, *, generated: str = "first") -> None:
    with h5py.File(path, "w") as handle:
        handle.attrs["stable"] = "yes"
        handle.create_dataset("values", data=values)
        metadata = handle.create_group("metadata")
        extraction = metadata.create_group("extraction")
        extraction.attrs["created_at"] = generated


def test_hdf5_exact_and_tolerance_boundaries(tmp_path: Path) -> None:
    expected = tmp_path / "expected.h5"
    actual = tmp_path / "actual.h5"
    values = np.array([1.0, np.nan, 3.0], dtype=np.float64)
    write_hdf5(expected, values, generated="first")
    write_hdf5(actual, values + np.array([1e-9, 0.0, 0.0]), generated="second")
    result = compare("extraction-h5", expected, actual, "extraction-v1")
    assert result.status == ComparisonStatus.EQUAL

    write_hdf5(actual, np.array([1.1, np.nan, 3.0]), generated="second")
    result = compare("extraction-h5", expected, actual, "extraction-v1")
    assert result.status == ComparisonStatus.DIFFERENT
    assert result.differences[0]["kind"] == "values"


def test_hdf5_integer_change_is_always_different(tmp_path: Path) -> None:
    expected = tmp_path / "expected.h5"
    actual = tmp_path / "actual.h5"
    write_hdf5(expected, np.array([1, 2], dtype=np.int32))
    write_hdf5(actual, np.array([1, 3], dtype=np.int32))
    assert compare("extraction-h5", expected, actual, "extraction-v1").status == "different"


def test_yaml_ignored_path_and_real_change(tmp_path: Path) -> None:
    expected = tmp_path / "expected.yml"
    actual = tmp_path / "actual.yml"
    expected.write_text("value: 1\noutput_dir: /first\n")
    actual.write_text("value: 1\noutput_dir: /second\n")
    assert compare("yaml", expected, actual, "yaml-v1").status == "equal"
    actual.write_text("value: 2\noutput_dir: /second\n")
    assert compare("yaml", expected, actual, "yaml-v1").status == "different"


def test_table_column_and_value_changes_are_structured(tmp_path: Path) -> None:
    expected = tmp_path / "expected.csv"
    actual = tmp_path / "actual.csv"
    pd.DataFrame({"a": [1, 2], "b": [3.0, 4.0]}).to_csv(expected, index=False)
    pd.DataFrame({"a": [1, 2], "b": [3.0, 5.0]}).to_csv(actual, index=False)
    result = compare("table", expected, actual, "table-v1")
    assert result.status == "different"
    assert result.differences[0]["path"] == "/b"


def test_pca_component_and_score_signs_are_aligned(tmp_path: Path) -> None:
    expected = tmp_path / "expected.npz"
    actual = tmp_path / "actual.npz"
    components = np.array([[1.0, 2.0], [3.0, -1.0]])
    scores = np.array([[1.0, 2.0], [3.0, 4.0]])
    variance = np.array([0.7, 0.2])
    np.savez(expected, components=components, scores=scores, explained_variance=variance)
    np.savez(
        actual,
        components=components * np.array([[-1.0], [1.0]]),
        scores=scores * np.array([[-1.0, 1.0]]),
        explained_variance=variance,
    )
    assert compare("pca", expected, actual, "pca-v1").status == "equal"


def test_controller_refuses_to_deserialize_model_pickle(tmp_path: Path) -> None:
    expected = tmp_path / "expected.pkl"
    actual = tmp_path / "actual.pkl"
    expected.write_bytes(b"not deserialized")
    actual.write_bytes(b"not deserialized")
    result = compare("trusted-model", expected, actual, "model-v1")
    assert result.status == "invalid"
    assert "not pickle bytes" in result.summary


def test_image_structure_and_intentional_change(tmp_path: Path) -> None:
    expected = tmp_path / "expected.png"
    actual = tmp_path / "actual.png"
    Image.new("RGB", (2, 2), "red").save(expected)
    Image.new("RGB", (3, 2), "red").save(actual)
    difference = compare("image", expected, actual, "image-v1")
    assert difference.status == "different"
    change = IntentionalChange(
        id="approved-image-size",
        old_expectation="2x2",
        new_expectation="3x2",
        affected_artifacts=["image"],
        issue_or_pr="https://github.com/dattalab/moseq2-test/issues/1",
        rationale="synthetic test",
        regression_test="tests/test_comparators.py",
        reviewer="maintainer",
        approved_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    accepted = compare("image", expected, actual, "image-v1", intentional_change=change)
    assert accepted.status == "expected-change"


def test_worker_generated_model_json_is_compared(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    actual = tmp_path / "actual.json"
    value = {"kind": "pickle", "structure": {"states": [1, 2, 3]}}
    expected.write_text(json.dumps(value))
    actual.write_text(json.dumps(value))
    assert compare("trusted-model", expected, actual, "model-v1").status == "equal"
