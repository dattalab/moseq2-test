"""Resource discovery and safe configuration loading."""

from __future__ import annotations

from contextlib import AbstractContextManager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import yaml

from moseq2_test.errors import InvalidConfiguration


def repository_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file():
        return candidate
    raise InvalidConfiguration("repository root is unavailable in this installation")


def resource(category: str, *parts: str) -> AbstractContextManager[Path]:
    """Yield an installed or source-tree resource as a filesystem path."""
    packaged = files("moseq2_test").joinpath("resources", category, *parts)
    if packaged.is_file() or packaged.is_dir():
        return as_file(packaged)
    local = repository_root().joinpath(category, *parts)
    return _local_context(local)


class _local_context:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_args: object) -> None:
        return None


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise InvalidConfiguration(f"cannot load YAML {path}: {error}") from error
    if not isinstance(value, dict):
        raise InvalidConfiguration(f"expected a mapping in {path}")
    return value
