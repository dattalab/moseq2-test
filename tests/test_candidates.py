from __future__ import annotations

import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from moseq2_test.candidates import (
    _build_distributions,
    _stage_locked_eigen,
    build_sources,
    inspect_wheel,
    verify_candidate_set,
    verify_installed_locations,
)
from moseq2_test.errors import InvalidConfiguration, MissingInput
from moseq2_test.models import CandidateKind, CandidateRecord, CandidateSet
from moseq2_test.provenance import sha256_file


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


def _write_eigen_archive(path: Path, member_name: str = "eigen-3.3.7/Eigen/Core") -> None:
    contents = b"locked Eigen header\n"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))


def _patch_eigen_lock(
    monkeypatch: pytest.MonkeyPatch, archive: Path, *, sha256: str | None = None
) -> None:
    monkeypatch.setenv("MOSEQ2_TEST_EXTERNAL_SOURCE_MIRROR", str(archive.parent))
    monkeypatch.setattr(
        "moseq2_test.candidates._locked_eigen_record",
        lambda: {
            "filename": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256 or sha256_file(archive),
        },
    )


def test_locked_eigen_is_staged_for_historical_compiled_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "eigen.tar.gz"
    _write_eigen_archive(archive)
    _patch_eigen_lock(monkeypatch, archive)
    export = tmp_path / "export"
    export.mkdir()

    evidence = _stage_locked_eigen("pyhsmm-autoregressive", export)

    assert (export / "deps" / "Eigen" / "Core").read_bytes() == b"locked Eigen header\n"
    assert evidence is not None
    assert evidence["sha256"] == sha256_file(archive)


def test_locked_eigen_hash_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "eigen.tar.gz"
    _write_eigen_archive(archive)
    _patch_eigen_lock(monkeypatch, archive, sha256="0" * 64)
    export = tmp_path / "export"
    export.mkdir()

    with pytest.raises(MissingInput, match="SHA-256"):
        _stage_locked_eigen("pyhsmm", export)


def test_locked_eigen_archive_traversal_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "eigen.tar.gz"
    _write_eigen_archive(archive, "eigen-3.3.7/Eigen/../../escape")
    _patch_eigen_lock(monkeypatch, archive)
    export = tmp_path / "export"
    export.mkdir()

    with pytest.raises(InvalidConfiguration, match="unsafe Eigen archive member"):
        _stage_locked_eigen("pyhsmm-autoregressive", export)


def test_candidate_provided_eigen_is_not_replaced(tmp_path: Path) -> None:
    export = tmp_path / "export"
    core = export / "deps" / "Eigen" / "Core"
    core.parent.mkdir(parents=True)
    core.write_bytes(b"candidate header\n")

    evidence = _stage_locked_eigen("pyhsmm-autoregressive", export)

    assert evidence == {"source": "candidate", "destination": "deps/Eigen"}
    assert core.read_bytes() == b"candidate header\n"


def test_candidate_eigen_symlink_is_rejected(tmp_path: Path) -> None:
    export = tmp_path / "export"
    external = tmp_path / "external"
    external.mkdir()
    (external / "Core").write_bytes(b"outside source export\n")
    (export / "deps").mkdir(parents=True)
    (export / "deps" / "Eigen").symlink_to(external, target_is_directory=True)

    with pytest.raises(InvalidConfiguration, match="symbolic link"):
        _stage_locked_eigen("pyhsmm-autoregressive", export)


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
