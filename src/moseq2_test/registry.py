"""Load and validate built-in versioned resources."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from moseq2_test.config import load_yaml, resource
from moseq2_test.errors import InvalidConfiguration, ProfileUnavailable
from moseq2_test.models import (
    FixtureManifest,
    KnownFailuresManifest,
    SourceLock,
    SuiteProfile,
    WheelLock,
)


def _validated[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(load_yaml(path))
    except ValidationError as error:
        raise InvalidConfiguration(f"{path} does not satisfy {model.__name__}: {error}") from error


def profile(name: str, *, require_implemented: bool = True) -> SuiteProfile:
    with resource("profiles", f"{name}.yml") as path:
        if not path.is_file():
            raise InvalidConfiguration(f"unknown profile: {name}")
        result = _validated(path, SuiteProfile)
    if require_implemented and not result.implemented:
        raise ProfileUnavailable(f"profile {name!r} is defined but unavailable")
    return result


def profiles() -> list[SuiteProfile]:
    with resource("profiles") as root:
        return [_validated(path, SuiteProfile) for path in sorted(root.glob("*.yml"))]


def fixture_manifest(name: str) -> FixtureManifest:
    with resource("manifests", "fixtures", f"{name}.yml") as path:
        if not path.is_file():
            raise InvalidConfiguration(f"unknown fixture set: {name}")
        return _validated(path, FixtureManifest)


def source_lock(name: str) -> SourceLock:
    with resource("manifests", "sources", f"{name}.yml") as path:
        if not path.is_file():
            raise InvalidConfiguration(f"unknown source lock: {name}")
        return _validated(path, SourceLock)


def wheel_lock(name: str) -> WheelLock:
    with resource("manifests", "wheels", f"{name}.yml") as path:
        if not path.is_file():
            raise InvalidConfiguration(f"unknown wheel lock: {name}")
        return _validated(path, WheelLock)


def known_failures() -> KnownFailuresManifest:
    with resource("manifests", "known-failures.yml") as path:
        return _validated(path, KnownFailuresManifest)
