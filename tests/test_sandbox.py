from pathlib import Path

import pytest

from moseq2_test.errors import InvalidConfiguration
from moseq2_test.sandbox import Sandbox, assert_unchanged, snapshot_tree, stage_read_only


def test_sandbox_roots_are_separate_and_logical(tmp_path: Path) -> None:
    sandbox = Sandbox.create(tmp_path)
    roots = {
        sandbox.inputs,
        sandbox.sources,
        sandbox.build,
        sandbox.wheelhouse,
        sandbox.target_env,
        sandbox.work,
        sandbox.result,
    }
    assert len(roots) == 7
    assert all(path.parent == sandbox.root for path in roots)
    assert sandbox.logical_path(sandbox.inputs / "a") == "inputs/a"
    sandbox.cleanup()
    assert not sandbox.root.exists()


def test_logical_path_rejects_external_paths(tmp_path: Path) -> None:
    sandbox = Sandbox.create(tmp_path)
    with pytest.raises(InvalidConfiguration):
        sandbox.logical_path(tmp_path.parent)


def test_staged_inputs_are_read_only_and_detect_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("original")
    sandbox = Sandbox.create(tmp_path / "workspace")
    staged = stage_read_only(source, sandbox.inputs / "fixture")
    before = snapshot_tree(staged)
    assert before[0].sha256
    assert (staged / "value.txt").stat().st_mode & 0o222 == 0
    (staged / "value.txt").chmod(0o644)
    (staged / "value.txt").write_text("changed")
    with pytest.raises(InvalidConfiguration):
        assert_unchanged(staged, before)
