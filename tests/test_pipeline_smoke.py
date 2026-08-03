from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np

from moseq2_test.registry import profile
from moseq2_test.suites.pipeline_smoke import (
    SELECTED_UUIDS,
    _step_result,
    make_compatible_recording,
    make_reduced_extraction,
    make_reduced_scores,
)


def test_profile_preserves_all_28_compact_pipeline_steps() -> None:
    selected = profile("pipeline-smoke", require_implemented=False)
    assert selected.implemented is True
    assert len(selected.steps) == 28
    assert selected.steps[0].id == "prepare-compact-real-data-project"
    assert selected.steps[-1].id == "run-2-app-noninteractive-smoke"
    assert selected.steps[1].expected_failure == "pipeline-pristine-good-timestamp-mismatch"
    assert "selected-recordings--good-session-tar-gz" in selected.steps[0].fixtures
    by_id = {step.id: step for step in selected.steps}
    assert by_id["run-2-pristine-good-session-timestamp-qc"].depends_on == [
        "prepare-compact-real-data-project"
    ]
    assert set(by_id["run-2-reference-model-apply"].depends_on) == {
        "run-2-model-train-smoke",
        "run-1-model-train-smoke",
    }


def test_compatible_recording_truncates_timestamps_to_depth_frames(tmp_path: Path) -> None:
    source = tmp_path / "good-session"
    source.mkdir()
    (source / "metadata.json").write_text(json.dumps({"DepthResolution": [2, 1]}))
    (source / "depth.dat").write_bytes(bytes(range(12)))  # 3 two-pixel uint16 frames
    (source / "depth_ts.txt").write_text("0\n1\n2\n3\n")
    destination = tmp_path / "compatible"
    record = make_compatible_recording(source, destination)
    assert record["depth_frames"] == 3
    assert record["selected_timestamps"] == 3
    assert (destination / "depth_ts.txt").read_text().splitlines() == ["0", "1", "2"]
    assert (destination / "depth.dat").read_bytes() == (source / "depth.dat").read_bytes()


def test_reduced_extraction_preserves_metadata_and_frame_datasets(tmp_path: Path) -> None:
    source = tmp_path / "source.h5"
    destination = tmp_path / "reduced.h5"
    with h5py.File(source, "w") as handle:
        handle.attrs["version"] = "test"
        metadata = handle.create_group("metadata")
        metadata.create_dataset("uuid", data=np.bytes_("abc"))
        frames = handle.create_group("frames")
        frames.attrs["units"] = "mm"
        frames.create_dataset("depth", data=np.arange(20).reshape(5, 4))
        handle.create_dataset("timestamps", data=np.arange(5))
    make_reduced_extraction(source, destination)
    with h5py.File(destination, "r") as handle:
        assert handle.attrs["version"] == "test"
        assert handle["metadata"]["uuid"][()] == b"abc"
        np.testing.assert_array_equal(handle["frames"]["depth"][:], np.arange(20).reshape(5, 4))
        np.testing.assert_array_equal(handle["timestamps"][:], np.arange(5))


def test_reduced_scores_selects_only_the_four_locked_sessions(tmp_path: Path) -> None:
    source = tmp_path / "scores.h5"
    destination = tmp_path / "reduced.h5"
    with h5py.File(source, "w") as handle:
        scores = handle.create_group("scores")
        indices = handle.create_group("scores_idx")
        metadata = handle.create_group("metadata")
        for index, uuid in enumerate(SELECTED_UUIDS):
            scores.create_dataset(uuid, data=np.full((4, 3), index, dtype=np.float32))
            indices.create_dataset(uuid, data=np.arange(4))
            group = metadata.create_group(uuid)
            group.attrs["index"] = index
        scores.create_dataset("not-selected", data=np.zeros((4, 3)))
        indices.create_dataset("not-selected", data=np.arange(4))
        metadata.create_group("not-selected")
    records = make_reduced_scores(source, destination)
    assert [record["uuid"] for record in records] == list(SELECTED_UUIDS)
    with h5py.File(destination, "r") as handle:
        assert set(handle["scores"]) == set(SELECTED_UUIDS)
        assert set(handle["scores_idx"]) == set(SELECTED_UUIDS)
        assert set(handle["metadata"]) == set(SELECTED_UUIDS)


def test_pipeline_expected_failure_requires_exact_exception_and_signature() -> None:
    matching = {
        "returncode": 1,
        "exception": "ValueError",
        "message": (
            "Frame count mismatch: depth video has 900 frames but there are 8979 timestamps"
        ),
    }
    accepted, policy = _step_result(
        "run-1-pristine-good-session-timestamp-qc",
        ["extract"],
        matching,
        0.1,
        expected_failure="pipeline-pristine-good-timestamp-mismatch",
    )
    assert accepted.classification == "known-failure"
    assert policy is not None
    assert policy["signature_matched"] is True

    substituted = dict(matching, exception="RuntimeError")
    rejected, policy = _step_result(
        "run-1-pristine-good-session-timestamp-qc",
        ["extract"],
        substituted,
        0.1,
        expected_failure="pipeline-pristine-good-timestamp-mismatch",
    )
    assert rejected.classification == "failed"
    assert policy is not None
    assert policy["signature_matched"] is False


def test_changepoints_advertised_failure_matches_sealed_exception_type() -> None:
    record, policy = _step_result(
        "run-1-changepoints-nodask-advertised-failure",
        ["compute-changepoints"],
        {
            "returncode": 1,
            "exception": "NotImplementedError",
            "message": ('Specified cluster not supported. Supported types are: "slurm", "local"'),
        },
        0.1,
        expected_failure="pipeline-changepoints-nodask-not-implemented",
    )
    assert record.classification == "known-failure"
    assert policy is not None
    assert policy["signature_matched"] is True
