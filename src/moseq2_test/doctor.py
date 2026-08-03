"""Host and executor preflight checks."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _tool(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"name": name, "available": path is not None, "path": path}


def inspect_host(workspace: Path, *, target_python: Path | None = None) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(workspace)
    target: dict[str, Any] | None = None
    if target_python is not None:
        if target_python.is_file():
            probe = (
                "import json,sys; print(json.dumps({'version':sys.version,'prefix':sys.prefix}))"
            )
            completed = subprocess.run(
                [
                    str(target_python),
                    "-c",
                    probe,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            target = {
                "path": str(target_python),
                "returncode": completed.returncode,
                "details": json.loads(completed.stdout) if completed.returncode == 0 else None,
                "stderr": completed.stderr.strip(),
            }
        else:
            target = {"path": str(target_python), "returncode": None, "missing": True}
    return {
        "controller": {"executable": sys.executable, "version": sys.version},
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "workspace": str(workspace.resolve()),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "tools": [_tool(name) for name in ("git", "ffprobe", "curl", "aws", "docker", "podman")],
        "target_python": target,
        "ci": os.environ.get("CI") == "true",
    }
