from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from moseq2_test.errors import InvalidConfiguration, Moseq2TestError
from moseq2_test.execution.container import container_command
from moseq2_test.execution.process import execute_worker
from moseq2_test.execution.protocol import WorkerRequest


def test_probe_round_trip_uses_json_protocol(tmp_path: Path) -> None:
    request = WorkerRequest(request_id="probe", operation="probe", parameters={})
    response = execute_worker(Path(sys.executable), request, work=tmp_path, timeout=30)
    assert response.protocol_version == 1
    assert response.result is not None
    assert response.result["python"]["executable"] == sys.executable


def test_unknown_worker_operation_fails_closed(tmp_path: Path) -> None:
    request = WorkerRequest(request_id="unknown", operation="not-real", parameters={})
    with pytest.raises(Moseq2TestError, match="failed"):
        execute_worker(Path(sys.executable), request, work=tmp_path, timeout=30)


def test_container_requires_digest_pin(tmp_path: Path) -> None:
    with pytest.raises(InvalidConfiguration, match="digest"):
        container_command("docker", "ghcr.io/dattalab/worker:latest", tmp_path, ["probe"])
    command = container_command(
        "docker", "ghcr.io/dattalab/worker@sha256:" + "a" * 64, tmp_path, ["probe"]
    )
    assert "--network=none" in command
    assert "--read-only" in command


def test_worker_has_python37_compatible_syntax() -> None:
    legacy_python = Path(
        "/n/groups/datta/john/projects/user-support/2026-08-02-moseq2-modernization/"
        "environments/2026-08-02_moseq2_modernization/legacy_py37/bin/python"
    )
    if not legacy_python.is_file():
        pytest.skip("sealed Python 3.7 interpreter is unavailable")
    worker = Path(__file__).parents[1] / "src/moseq2_test/workers/legacy_worker.py"
    subprocess.run([str(legacy_python), "-m", "py_compile", str(worker)], check=True)


def test_seeded_cli_returns_structured_failure_and_uses_requested_cwd(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    (module_root / "failing_cli.py").write_text(
        "import os\ndef cli():\n    raise ValueError('worker cwd: ' + os.getcwd())\n",
        encoding="utf-8",
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    request = WorkerRequest(
        request_id="structured-failure",
        operation="seeded-cli",
        parameters={
            "module": "failing_cli",
            "arguments": [],
            "seed": 7,
            "cwd": str(run_root),
        },
    )
    response = execute_worker(
        Path(sys.executable),
        request,
        work=tmp_path / "worker",
        timeout=30,
        environment={"PYTHONPATH": str(module_root)},
    )
    assert response.result is not None
    assert response.result["returncode"] == 1
    assert response.result["exception"] == "ValueError"
    assert response.result["message"] == f"worker cwd: {run_root}"
