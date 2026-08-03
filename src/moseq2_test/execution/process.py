"""Process executor for an explicit target Python."""

from __future__ import annotations

import json
import os
import subprocess
from importlib.resources import as_file, files
from pathlib import Path

from pydantic import ValidationError

from moseq2_test.errors import InvalidConfiguration, MissingInput, Moseq2TestError
from moseq2_test.execution.protocol import WorkerRequest, WorkerResponse


def execute_worker(
    target_python: Path,
    request: WorkerRequest,
    *,
    work: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> WorkerResponse:
    if not target_python.is_file():
        raise MissingInput(f"target Python does not exist: {target_python}")
    work.mkdir(parents=True, exist_ok=True)
    request_path = work / f"{request.request_id}.request.json"
    response_path = work / f"{request.request_id}.response.json"
    request_path.write_text(
        json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    worker_resource = files("moseq2_test.workers").joinpath("legacy_worker.py")
    with as_file(worker_resource) as worker:
        command = [
            str(target_python),
            str(worker),
            "--request",
            str(request_path),
            "--response",
            str(response_path),
        ]
        merged_environment = dict(os.environ)
        if environment:
            merged_environment.update(environment)
        try:
            completed = subprocess.run(
                command,
                cwd=work,
                env=merged_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise Moseq2TestError(f"worker timed out after {timeout} seconds") from error
    (work / f"{request.request_id}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (work / f"{request.request_id}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if not response_path.is_file():
        raise Moseq2TestError(
            f"worker produced no response (exit {completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        response = WorkerResponse.model_validate_json(response_path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise InvalidConfiguration(f"malformed worker response: {error}") from error
    if response.request_id != request.request_id:
        raise InvalidConfiguration("worker response request ID mismatch")
    if completed.returncode != 0 or response.status != "ok":
        raise Moseq2TestError(f"worker operation {request.operation} failed: {response.error}")
    return response
