from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from moseq2_test.candidates import (
    _build_distributions,
    build_sources,
    inspect_wheel,
    verify_candidate_set,
    verify_installed_locations,
)
from moseq2_test.errors import InvalidConfiguration
from moseq2_test.models import CandidateKind, CandidateRecord, CandidateSet


def synthetic_source(root: Path) -> Path:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "moseq2-extract"
version = "9.0.0"
""".strip()
        + "\n"
    )
    package = root / "moseq2_extract"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "9.0.0"\n')
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "synthetic"], cwd=root, check=True, capture_output=True)
    return root


def test_source_candidate_builds_one_noneditable_wheel(tmp_path: Path) -> None:
    source = synthetic_source(tmp_path / "source")
    result = build_sources(
        [f"moseq2-extract={source}"],
        workspace=tmp_path / "workspace",
        output=tmp_path / "output",
        allow_dirty=False,
    )
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.sha256
    assert candidate.dirty is False
    assert Path(candidate.test_source or "").is_dir()
    verify_candidate_set(result, base=tmp_path)


def test_explicit_python_uses_native_legacy_build_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = _build_distributions(
        python=Path("/target/bin/python"),
        source=tmp_path / "source",
        output=tmp_path / "dist",
        legacy_setup=True,
    )
    assert [result.returncode for result in results] == [0, 0]
    assert commands[0][1:3] == ["setup.py", "sdist"]
    assert commands[1][1:6] == ["-m", "pip", "wheel", "--no-deps", "--no-build-isolation"]


def test_dirty_source_is_rejected_unless_explicitly_allowed(tmp_path: Path) -> None:
    source = synthetic_source(tmp_path / "source")
    (source / "moseq2_extract" / "__init__.py").write_text('__version__ = "dirty"\n')
    with pytest.raises(InvalidConfiguration, match="dirty"):
        build_sources(
            [f"moseq2-extract={source}"],
            workspace=tmp_path / "workspace",
            output=tmp_path / "output",
            allow_dirty=False,
        )


def test_editable_direct_url_in_wheel_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "moseq2_extract-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "moseq2_extract-1.0.dist-info/direct_url.json",
            json.dumps({"url": "file:///source", "dir_info": {"editable": True}}),
        )
    with pytest.raises(InvalidConfiguration, match="editable"):
        inspect_wheel(wheel, expected_package="moseq2-extract")


def test_misnamed_wheel_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "different-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("different/__init__.py", "")
    with pytest.raises(InvalidConfiguration, match="expected"):
        inspect_wheel(wheel, expected_package="moseq2-extract")


def test_candidate_manifest_with_wrong_wheel_hash_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "moseq2_extract-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("moseq2_extract/__init__.py", "")
    candidate_set = CandidateSet(
        candidates=[
            CandidateRecord(
                package="moseq2-extract",
                kind=CandidateKind.WHEEL,
                location=str(wheel),
                sha256="0" * 64,
            )
        ]
    )
    with pytest.raises(InvalidConfiguration, match="hash differs"):
        verify_candidate_set(candidate_set, base=tmp_path)


def test_installed_import_cannot_resolve_to_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    imported = source / "moseq2_extract" / "__init__.py"
    imported.parent.mkdir(parents=True)
    imported.write_text("")
    response = {
        "imports": [{"module": "moseq2_extract", "imported": True, "file": str(imported)}],
        "distributions": [{"name": "moseq2-extract", "direct_url": None}],
    }
    with pytest.raises(InvalidConfiguration, match="forbidden root"):
        verify_installed_locations(response, forbidden_roots=[source])
