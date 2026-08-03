"""Clean, separated, auditable run sandboxes."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from moseq2_test.errors import InvalidConfiguration
from moseq2_test.provenance import sha256_file


@dataclass(frozen=True)
class TreeEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Sandbox:
    root: Path
    inputs: Path
    sources: Path
    build: Path
    wheelhouse: Path
    target_env: Path
    work: Path
    result: Path

    @classmethod
    def create(cls, workspace: Path, *, prefix: str = "moseq2-test-") -> Sandbox:
        workspace.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=workspace)).resolve()
        names = ("inputs", "sources", "build", "wheelhouse", "target-env", "work", "result")
        paths = {name.replace("-", "_"): root / name for name in names}
        for path in paths.values():
            path.mkdir()
        return cls(root=root, **paths)

    def logical_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError as error:
            raise InvalidConfiguration(f"path is outside sandbox: {path}") from error

    def cleanup(self) -> None:
        resolved = self.root.resolve()
        if resolved == Path(resolved.anchor) or not resolved.name.startswith("moseq2-test-"):
            raise InvalidConfiguration(f"refusing unsafe sandbox cleanup: {resolved}")
        shutil.rmtree(resolved)


def snapshot_tree(root: Path) -> list[TreeEntry]:
    if not root.exists():
        return []
    entries: list[TreeEntry] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        entries.append(
            TreeEntry(
                path=path.relative_to(root).as_posix(),
                size=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    return entries


def assert_unchanged(root: Path, before: list[TreeEntry]) -> None:
    after = snapshot_tree(root)
    if after != before:
        raise InvalidConfiguration(f"immutable tree changed during run: {root}")


def stage_read_only(source: Path, destination: Path) -> Path:
    if destination.exists():
        raise InvalidConfiguration(f"refusing to overwrite staged input: {destination}")
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise InvalidConfiguration(f"cannot stage missing input: {source}")
    paths = [destination, *destination.rglob("*")] if destination.is_dir() else [destination]
    for path in sorted(paths, reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    return destination
