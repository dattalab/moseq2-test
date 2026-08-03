"""Digest-pinned container command construction."""

from __future__ import annotations

from pathlib import Path

from moseq2_test.errors import InvalidConfiguration


def container_command(
    runtime: str,
    image: str,
    sandbox: Path,
    arguments: list[str],
) -> list[str]:
    if "@sha256:" not in image:
        raise InvalidConfiguration("container images must be pinned by SHA-256 digest")
    if runtime not in {"docker", "podman"}:
        raise InvalidConfiguration("container runtime must be docker or podman")
    return [
        runtime,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--mount",
        f"type=bind,src={sandbox.resolve()},dst=/work",
        "--workdir",
        "/work",
        image,
        *arguments,
    ]
