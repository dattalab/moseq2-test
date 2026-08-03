"""Canonical provenance capture and redaction."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SENSITIVE_FRAGMENTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "KEY")


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme:
        return value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redacted_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    source = environment if environment is not None else dict(os.environ)
    result: dict[str, str] = {}
    for name, value in sorted(source.items()):
        upper = name.upper()
        if any(fragment in upper for fragment in SENSITIVE_FRAGMENTS):
            result[name] = "<redacted>"
        elif value.startswith(("http://", "https://")):
            result[name] = redact_url(value)
        else:
            result[name] = value
    return result


def controller_environment() -> dict[str, object]:
    return {
        "python": {"version": sys.version, "implementation": platform.python_implementation()},
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }
