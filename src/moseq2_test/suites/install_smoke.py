"""Installed-package parity checks for the eight-package legacy stack."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moseq2_test import WORKER_PROTOCOL_VERSION, __version__
from moseq2_test.candidates import (
    build_sources,
    canonical_package,
    inspect_wheel,
    load_candidate_set,
    parse_assignment,
    verify_candidate_set,
)
from moseq2_test.config import load_yaml, resource
from moseq2_test.errors import ExitCode, InvalidConfiguration, MissingInput, Moseq2TestError
from moseq2_test.execution.process import execute_worker
from moseq2_test.execution.protocol import WorkerRequest
from moseq2_test.models import (
    CandidateKind,
    CandidateRecord,
    CommandResult,
    RunRecord,
    SuiteProfile,
    WheelLock,
)
from moseq2_test.provenance import controller_environment, redacted_environment, sha256_file
from moseq2_test.registry import source_lock, wheel_lock
from moseq2_test.reporting import write_run_directory
from moseq2_test.sandbox import Sandbox

IMPORTS = (
    "moseq2_extract",
    "moseq2_pca",
    "moseq2_model",
    "moseq2_viz",
    "moseq2_app",
    "pybasicbayes",
    "pyhsmm",
    "autoregressive",
)
DISTRIBUTIONS = (
    "moseq2-extract",
    "moseq2-pca",
    "moseq2-model",
    "moseq2-viz",
    "moseq2-app",
    "pybasicbayes",
    "pyhsmm",
    "autoregressive",
)
EXTENSIONS = (
    "pybasicbayes.util.cstats",
    "pyhsmm.internals.hmm_messages_interface",
    "pyhsmm.internals.hsmm_messages_interface",
    "pyhsmm.util.cstats",
    "autoregressive.messages",
)
CONSOLE_SCRIPTS = ("moseq2-extract", "moseq2-pca", "moseq2-model", "moseq2-viz")


def _run_id(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _command_result(
    check_id: str,
    passed: bool,
    evidence: object,
    *,
    command: list[str] | None = None,
    returncode: int | None = None,
    duration: float = 0.0,
    stderr: str | None = None,
) -> CommandResult:
    return CommandResult(
        id=check_id,
        command=command or ["moseq2-test", "check", check_id],
        returncode=(0 if passed else 1) if returncode is None else returncode,
        duration_seconds=max(duration, 0.0),
        stdout=_json(evidence),
        stderr=stderr,
        classification="passed" if passed else "failed",
    )


def _within(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def _selected_ids(
    profile: SuiteProfile, *, explicit: list[str], start: str | None, through: str | None
) -> list[str]:
    if start or through:
        raise InvalidConfiguration(
            "--start-at/--through apply to pipeline profiles, not install-smoke"
        )
    all_ids = [step.id for step in profile.steps]
    if not explicit:
        return all_ids
    unknown = sorted(set(explicit) - set(all_ids))
    if unknown:
        raise InvalidConfiguration(f"unknown install-smoke steps: {', '.join(unknown)}")
    requested = set(explicit)
    return [check_id for check_id in all_ids if check_id in requested]


def _artifact_roots(target_prefix: Path) -> dict[str, Path]:
    default_root = target_prefix.parent
    return {
        "source_archive": Path(
            os.environ.get("MOSEQ2_TEST_SOURCE_ARCHIVE_MIRROR", default_root / "source_archives")
        ).expanduser(),
        "sdist": Path(
            os.environ.get("MOSEQ2_TEST_SDIST_MIRROR", default_root / "sdists")
        ).expanduser(),
        "wheel": Path(
            os.environ.get("MOSEQ2_TEST_WHEEL_MIRROR", default_root / "wheels")
        ).expanduser(),
        "external": Path(
            os.environ.get("MOSEQ2_TEST_EXTERNAL_SOURCE_MIRROR", default_root / "external_sources")
        ).expanduser(),
    }


def _file_evidence(
    path: Path, *, expected_hash: str, expected_size: int | None = None
) -> tuple[bool, dict[str, object]]:
    if not path.is_file():
        return False, {
            "logical_path": path.name,
            "present": False,
            "expected_sha256": expected_hash,
        }
    actual_hash = sha256_file(path)
    size = path.stat().st_size
    passed = actual_hash == expected_hash and (expected_size is None or size == expected_size)
    return passed, {
        "logical_path": path.name,
        "present": True,
        "size": size,
        "expected_size": expected_size,
        "sha256": actual_hash,
        "expected_sha256": expected_hash,
    }


def _baseline_candidates(lock: WheelLock, wheel_root: Path) -> list[CandidateRecord]:
    records: list[CandidateRecord] = []
    for item in lock.wheels:
        wheel = wheel_root / item.filename
        passed, _ = _file_evidence(wheel, expected_hash=item.sha256)
        if not passed:
            raise MissingInput(f"missing or invalid locked baseline wheel: {wheel}")
        inspect_wheel(wheel, expected_package=item.package)
        records.append(
            CandidateRecord(
                package=item.package,
                kind=CandidateKind.WHEEL,
                location=f"wheelhouse/{item.filename}",
                sha256=item.sha256,
                source_commit=item.source_commit,
                sdist_location=f"sdists/{item.sdist_filename}",
                sdist_sha256=item.sdist_sha256,
            )
        )
    return records


def _explicit_candidates(values: list[str], candidate_set: Path | None) -> list[CandidateRecord]:
    records: list[CandidateRecord] = []
    if candidate_set is not None:
        loaded = load_candidate_set(candidate_set)
        verify_candidate_set(loaded, base=candidate_set.parent)
        for record in loaded.candidates:
            location = Path(record.location)
            if not location.is_absolute():
                location = (candidate_set.parent / location).resolve()
            sdist_location = record.sdist_location
            if sdist_location and not Path(sdist_location).is_absolute():
                sdist_location = str((candidate_set.parent / sdist_location).resolve())
            records.append(
                record.model_copy(
                    update={"location": str(location), "sdist_location": sdist_location}
                )
            )
    for value in values:
        name, path = parse_assignment(value)
        details = inspect_wheel(path, expected_package=name)
        records.append(
            CandidateRecord(
                package=name,
                kind=CandidateKind.WHEEL,
                location=str(path),
                sha256=str(details["sha256"]),
            )
        )
    names = [canonical_package(record.package) for record in records]
    if len(names) != len(set(names)):
        raise InvalidConfiguration("duplicate candidate package definitions")
    return records


def _apply_test_sources(records: list[CandidateRecord], values: list[str]) -> list[CandidateRecord]:
    overrides = {name: path for name, path in (parse_assignment(value) for value in values)}
    known = {canonical_package(record.package) for record in records}
    unknown = set(overrides) - known
    if unknown:
        raise InvalidConfiguration(
            f"test-source override has no resolved package: {sorted(unknown)}"
        )
    return [
        record.model_copy(update={"test_source": str(overrides[canonical_package(record.package)])})
        if canonical_package(record.package) in overrides
        else record
        for record in records
    ]


def _create_layered_target(
    base_python: Path,
    sandbox: Sandbox,
    candidates: list[CandidateRecord],
    timeout: int,
    *,
    force: bool = False,
) -> Path:
    if not candidates and not force:
        return base_python
    target = sandbox.target_env / "runtime"
    completed = subprocess.run(
        [str(base_python), "-m", "venv", "--system-site-packages", str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    (sandbox.result / "target-environment.stdout.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    (sandbox.result / "target-environment.stderr.log").write_text(
        completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise InvalidConfiguration("could not create disposable target environment")
    python = target / "bin" / "python"
    for record in candidates:
        wheel = Path(record.location)
        completed = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                "--force-reinstall",
                str(wheel),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        (sandbox.result / f"install-{record.package}.stdout.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        (sandbox.result / f"install-{record.package}.stderr.log").write_text(
            completed.stderr, encoding="utf-8"
        )
        if completed.returncode != 0:
            raise InvalidConfiguration(f"candidate wheel installation failed: {record.package}")
    return python


def _inspect(
    target_python: Path,
    sandbox: Sandbox,
    *,
    timeout: int,
) -> dict[str, Any]:
    request = WorkerRequest(
        request_id="inspect-installation",
        operation="inspect-installation",
        parameters={
            "imports": list(IMPORTS),
            "distributions": list(DISTRIBUTIONS),
            "extensions": list(EXTENSIONS),
            "console_scripts": list(CONSOLE_SCRIPTS),
        },
    )
    response = execute_worker(target_python, request, work=sandbox.work, timeout=timeout)
    if response.result is None:
        raise InvalidConfiguration("worker installation inspection returned no result")
    return response.result


def _record_by(records: object, key: str, value: str) -> dict[str, Any]:
    if not isinstance(records, list):
        return {key: value, "malformed": True}
    for item in records:
        if isinstance(item, dict) and item.get(key) == value:
            return item
    return {key: value, "missing": True}


def _inspection_results(
    selected: set[str],
    inspection: dict[str, Any],
    *,
    expected_prefix: Path,
    allowed_roots: list[Path],
    pip_check: dict[str, Any],
) -> dict[str, CommandResult]:
    results: dict[str, CommandResult] = {}
    version_info = inspection.get("python_version_info")
    passed = isinstance(version_info, list) and version_info[:2] == [3, 7]
    results["python-version"] = _command_result(
        "python-version",
        passed,
        {
            "version": inspection.get("python_version"),
            "executable": inspection.get("python_executable"),
        },
    )
    actual_prefix = Path(str(inspection.get("python_prefix", ""))).resolve()
    pip_ok = pip_check.get("returncode") == 0
    results["python-prefix"] = _command_result(
        "python-prefix",
        actual_prefix == expected_prefix.resolve() and pip_ok,
        {"expected": "target-environment", "actual": str(actual_prefix), "pip_check": pip_check},
    )
    editable = inspection.get("editable_links")
    results["no-editable-links"] = _command_result(
        "no-editable-links", isinstance(editable, list) and not editable, editable
    )
    escaped: list[dict[str, Any]] = []
    raw_pth = inspection.get("pth_entries")
    if isinstance(raw_pth, list):
        for entry in raw_pth:
            if not isinstance(entry, dict):
                escaped.append({"malformed": entry})
                continue
            value = entry.get("entry")
            if (
                isinstance(value, str)
                and Path(value).is_absolute()
                and not _within(Path(value), allowed_roots)
            ):
                escaped.append(entry)
    else:
        escaped.append({"malformed": raw_pth})
    results["no-escaped-pth"] = _command_result("no-escaped-pth", not escaped, escaped)

    for name in IMPORTS:
        check_id = f"import-{name}"
        record = _record_by(inspection.get("imports"), "module", name)
        raw_path = record.get("file")
        good_path = isinstance(raw_path, str) and _within(Path(raw_path), allowed_roots)
        results[check_id] = _command_result(
            check_id, record.get("imported") is True and good_path, record
        )
    for name in DISTRIBUTIONS:
        check_id = f"distribution-{name}"
        record = _record_by(inspection.get("distributions"), "name", name)
        metadata_path = record.get("metadata_path")
        direct_url = record.get("direct_url")
        editable_install = (
            isinstance(direct_url, dict) and direct_url.get("dir_info", {}).get("editable") is True
        )
        good_path = isinstance(metadata_path, str) and _within(Path(metadata_path), allowed_roots)
        results[check_id] = _command_result(
            check_id, record.get("installed") is True and good_path and not editable_install, record
        )
    for name in EXTENSIONS:
        check_id = f"extension-{name}"
        record = _record_by(inspection.get("extensions"), "module", name)
        raw_path = record.get("file")
        good_path = isinstance(raw_path, str) and _within(Path(raw_path), allowed_roots)
        results[check_id] = _command_result(
            check_id,
            record.get("imported") is True and record.get("compiled") is True and good_path,
            record,
        )
    return {key: value for key, value in results.items() if key in selected}


def _worker_command(
    target_python: Path,
    sandbox: Sandbox,
    *,
    request_id: str,
    command: list[str],
    timeout: int,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    response = execute_worker(
        target_python,
        WorkerRequest(
            request_id=request_id,
            operation="run-command",
            parameters={"command": command, "timeout": timeout},
        ),
        work=sandbox.work,
        timeout=timeout + 10,
    )
    if response.result is None:
        raise InvalidConfiguration(f"worker command {request_id} returned no result")
    return response.result, time.monotonic() - started


def _cli_result(
    check_id: str,
    *,
    path: Path,
    argument: str,
    script_record: dict[str, Any],
    worker_result: dict[str, Any] | None = None,
    duration: float = 0.0,
) -> CommandResult:
    command = [str(path), argument]
    if script_record.get("exists") is not True:
        return _command_result(check_id, False, script_record, command=command)
    result = worker_result or {}
    returncode = result.get("returncode")
    output = str(result.get("stdout", "")) + str(result.get("stderr", ""))
    return _command_result(
        check_id,
        returncode == 0 and bool(output.strip()),
        result,
        command=command,
        returncode=int(returncode) if isinstance(returncode, int) else 1,
        duration=duration,
    )


def _copy_logs(sandbox: Sandbox, run_directory: Path) -> None:
    destination = run_directory / "logs"
    destination.mkdir(parents=True, exist_ok=True)
    for root in (sandbox.work, sandbox.result):
        for path in root.glob("*.log"):
            shutil.copy2(path, destination / path.name)
        for path in root.glob("*.request.json"):
            shutil.copy2(path, destination / path.name)
        for path in root.glob("*.response.json"):
            shutil.copy2(path, destination / path.name)


def run_install_smoke(options: Any, profile: SuiteProfile) -> tuple[Path, int]:
    """Run the exact 47-check installation contract and persist its evidence."""
    started_at = datetime.now(UTC)
    run_id = _run_id(started_at)
    run_directory = (options.output_dir / run_id).resolve()
    selected_ids = _selected_ids(
        profile, explicit=options.steps, start=options.start_at, through=options.through
    )
    selected = set(selected_ids)
    timeout = options.timeout or profile.resources.timeout_seconds
    if timeout > profile.resources.timeout_seconds:
        raise InvalidConfiguration("--timeout cannot exceed the profile certification ceiling")
    if options.keep_sandbox not in {"always", "failure", "never"}:
        raise InvalidConfiguration("--keep-sandbox must be always, failure, or never")
    if options.executor != "process":
        raise InvalidConfiguration("install-smoke container execution is unavailable until P10")
    if options.target_python is None:
        raise MissingInput("install-smoke process execution requires --target-python")
    # Preserve a venv entry point rather than resolving its interpreter symlink;
    # resolving it would silently discard the venv's pyvenv.cfg and overlays.
    base_python = options.target_python.expanduser().absolute()
    if not base_python.is_file():
        raise MissingInput(f"target Python does not exist: {base_python}")

    sandbox = Sandbox.create(options.workspace, prefix="moseq2-test-install-smoke-")
    commands: dict[str, CommandResult] = {}
    failure_stage: str | None = None
    environment: dict[str, Any] = {"controller": controller_environment()}
    provenance: dict[str, Any] = {"environment_variables": redacted_environment()}
    resolved_candidates: list[CandidateRecord] = []
    source = source_lock(options.baseline_lock)
    wheels = wheel_lock("moseq2-baseline-linux-py37-v1")
    actual_python = base_python
    try:
        base_prefix = base_python.parent.parent.resolve()
        roots = _artifact_roots(base_prefix)
        baseline_records = _baseline_candidates(wheels, roots["wheel"])
        candidate_records = _explicit_candidates(options.candidates, options.candidate_set)
        if options.sources:
            built = build_sources(
                options.sources,
                workspace=sandbox.build / "candidate-workspace",
                output=sandbox.build / "candidate-output",
                allow_dirty=options.allow_dirty_source,
                build_python=base_python,
            )
            candidate_records.extend(built.candidates)
            names = [canonical_package(item.package) for item in candidate_records]
            if len(names) != len(set(names)):
                raise InvalidConfiguration("duplicate source/candidate package definitions")
        overrides = {canonical_package(item.package): item for item in candidate_records}
        resolved_candidates = [
            overrides.get(canonical_package(item.package), item) for item in baseline_records
        ]
        resolved_candidates = _apply_test_sources(resolved_candidates, options.test_sources)
        unknown_overrides = set(overrides) - {
            canonical_package(item.package) for item in baseline_records
        }
        if unknown_overrides:
            raise InvalidConfiguration(
                f"candidate set has unknown packages: {sorted(unknown_overrides)}"
            )

        actual_python = _create_layered_target(base_python, sandbox, candidate_records, timeout)
        actual_prefix = actual_python.parent.parent.resolve()
        allowed_roots = [actual_prefix, base_prefix]

        source_by_name = {item.name: item for item in source.sources}
        lock_pairs_ok = len(source.sources) == len(wheels.wheels) == 8 and all(
            item.package in source_by_name
            and item.source_commit == source_by_name[item.package].commit
            for item in wheels.wheels
        )
        if "build-provenance-manifest" in selected:
            commands["build-provenance-manifest"] = _command_result(
                "build-provenance-manifest",
                lock_pairs_ok,
                {
                    "source_lock": source.lock_id,
                    "wheel_lock": wheels.lock_id,
                    "records": len(wheels.wheels),
                    "source_wheel_identities_match": lock_pairs_ok,
                },
            )
        resolved_by_name = {canonical_package(item.package): item for item in resolved_candidates}
        for item in wheels.wheels:
            check_id = f"build-artifacts-{item.package}"
            if check_id not in selected:
                continue
            resolved = resolved_by_name[canonical_package(item.package)]
            if canonical_package(item.package) in overrides:
                wheel_path = Path(resolved.location)
                wheel_ok, wheel_evidence = _file_evidence(
                    wheel_path, expected_hash=str(resolved.sha256)
                )
                if resolved.sdist_location and resolved.sdist_sha256:
                    sdist_ok, sdist_evidence = _file_evidence(
                        Path(resolved.sdist_location), expected_hash=resolved.sdist_sha256
                    )
                else:
                    sdist_ok, sdist_evidence = True, {"not_provided": True}
                source_ok = bool(resolved.source_commit or resolved.sha256)
                source_evidence: dict[str, object] = {
                    "source_commit": resolved.source_commit,
                    "wheel_only_candidate": resolved.source_commit is None,
                }
            else:
                source_ok, source_evidence = _file_evidence(
                    roots["source_archive"] / item.source_archive_filename,
                    expected_hash=item.source_archive_sha256,
                )
                sdist_ok, sdist_evidence = _file_evidence(
                    roots["sdist"] / item.sdist_filename, expected_hash=item.sdist_sha256
                )
                wheel_ok, wheel_evidence = _file_evidence(
                    roots["wheel"] / item.filename, expected_hash=item.sha256
                )
            commands[check_id] = _command_result(
                check_id,
                source_ok and sdist_ok and wheel_ok,
                {
                    "source_archive": source_evidence,
                    "sdist": sdist_evidence,
                    "wheel": wheel_evidence,
                },
            )

        if "external-build-source-eigen" in selected:
            with resource("environments", "external-sources.lock.yml") as external_lock_path:
                external = load_yaml(external_lock_path)["sources"][0]
            eigen_ok, eigen_evidence = _file_evidence(
                roots["external"] / external["filename"],
                expected_hash=external["sha256"],
                expected_size=external["size"],
            )
            commands["external-build-source-eigen"] = _command_result(
                "external-build-source-eigen", eigen_ok, eigen_evidence
            )

        inspection = _inspect(actual_python, sandbox, timeout=timeout)
        pip_check, pip_duration = _worker_command(
            actual_python,
            sandbox,
            request_id="pip-check",
            command=[str(actual_python), "-m", "pip", "check"],
            timeout=min(timeout, 300),
        )
        inspection_commands = _inspection_results(
            selected,
            inspection,
            expected_prefix=actual_prefix,
            allowed_roots=allowed_roots,
            pip_check=pip_check,
        )
        if "python-prefix" in inspection_commands:
            inspection_commands["python-prefix"] = inspection_commands["python-prefix"].model_copy(
                update={"duration_seconds": pip_duration}
            )
        commands.update(inspection_commands)

        console_by_name = {
            item["name"]: item
            for item in inspection.get("console_scripts", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        for command_name in CONSOLE_SCRIPTS:
            for argument in ("--help", "--version"):
                check_id = f"cli-{command_name}-{argument[2:]}"
                if check_id not in selected:
                    continue
                script = console_by_name.get(command_name, {})
                path = Path(str(script.get("path", actual_prefix / "bin" / command_name)))
                if not script.get("exists"):
                    commands[check_id] = _cli_result(
                        check_id,
                        path=path,
                        argument=argument,
                        script_record=script,
                    )
                    continue
                result, duration = _worker_command(
                    actual_python,
                    sandbox,
                    request_id=check_id,
                    command=[str(path), argument],
                    timeout=min(timeout, 60),
                )
                commands[check_id] = _cli_result(
                    check_id,
                    path=path,
                    argument=argument,
                    script_record=script,
                    worker_result=result,
                    duration=duration,
                )

        compiled_ids = {
            "compiled-operation-pybasicbayes",
            "compiled-operation-pyhsmm-cstats",
            "compiled-operation-pyhsmm-hmm",
            "compiled-operation-autoregressive",
        }
        if selected & compiled_ids:
            started = time.monotonic()
            response = execute_worker(
                actual_python,
                WorkerRequest(
                    request_id="compiled-smoke",
                    operation="compiled-smoke",
                    parameters={"seed": options.seed},
                ),
                work=sandbox.work,
                timeout=timeout,
            )
            duration = time.monotonic() - started
            raw_checks = response.result.get("checks", []) if response.result else []
            compiled_by_id = {
                item["id"]: item
                for item in raw_checks
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            for check_id in compiled_ids & selected:
                evidence = compiled_by_id.get(check_id, {"id": check_id, "missing": True})
                commands[check_id] = _command_result(
                    check_id,
                    evidence.get("passed") is True,
                    evidence,
                    command=[str(actual_python), "worker:compiled-smoke", check_id],
                    duration=duration,
                )

        environment.update(
            {
                "target": {
                    "python": inspection.get("python_executable"),
                    "version": inspection.get("python_version"),
                    "prefix": inspection.get("python_prefix"),
                    "pip_check": pip_check,
                },
                "locks": {"source": source.lock_id, "wheel": wheels.lock_id},
            }
        )
        provenance.update(
            {
                "selected_steps": selected_ids,
                "baseline_build_inputs": {
                    "source_archives": [item.source_archive_sha256 for item in wheels.wheels],
                    "sdists": [item.sdist_sha256 for item in wheels.wheels],
                    "wheels": [item.sha256 for item in wheels.wheels],
                },
                "display_paths": {
                    "base_python": str(base_python),
                    "target_python": str(actual_python),
                    "artifact_roots": {key: str(value.resolve()) for key, value in roots.items()},
                },
            }
        )
    except Moseq2TestError as error:
        failure_stage = "setup"
        commands.setdefault(
            "setup",
            _command_result(
                "setup",
                False,
                {"error_type": type(error).__name__, "error": str(error), "code": error.code},
            ),
        )
    except (
        Exception
    ) as error:  # preserve an auditable partial run for unexpected infrastructure faults
        failure_stage = "infrastructure"
        commands.setdefault(
            "infrastructure",
            _command_result(
                "infrastructure",
                False,
                {"error_type": type(error).__name__, "error": str(error)},
            ),
        )

    ordered_commands = [commands[check_id] for check_id in selected_ids if check_id in commands]
    ordered_commands.extend(
        result for check_id, result in commands.items() if check_id not in selected
    )
    missing = [check_id for check_id in selected_ids if check_id not in commands]
    for check_id in missing:
        ordered_commands.append(
            _command_result(
                check_id, False, {"missing_result": True}, stderr="runner omitted check"
            )
        )
    accepted = not failure_stage and all(
        item.classification == "passed" for item in ordered_commands
    )
    if not accepted and failure_stage is None:
        failure_stage = next(
            (
                next(step.stage for step in profile.steps if step.id == item.id)
                for item in ordered_commands
                if item.classification != "passed" and item.id in selected
            ),
            "run",
        )
    record = RunRecord(
        run_id=run_id,
        framework_version=__version__,
        worker_protocol_version=WORKER_PROTOCOL_VERSION,
        profile=profile.name,
        status="accepted" if accepted else "failed",
        failure_stage=failure_stage,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        seed=options.seed,
        source_lock=source.lock_id,
        wheel_lock=wheels.lock_id,
        fixture_sets=[],
        candidates=resolved_candidates,
        commands=ordered_commands,
        environment=environment,
        provenance=provenance,
    )
    write_run_directory(
        run_directory,
        record,
        resolved_config={
            "profile": profile.name,
            "baseline_lock": options.baseline_lock,
            "selected_steps": selected_ids,
            "seed": options.seed,
            "executor": options.executor,
        },
    )
    (run_directory / "source-lock.json").write_text(
        _json(source.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "wheel-lock.json").write_text(
        _json(wheels.model_dump(mode="json")), encoding="utf-8"
    )
    (run_directory / "manifests" / "suite-profile.json").write_text(
        _json(profile.model_dump(mode="json")), encoding="utf-8"
    )
    _copy_logs(sandbox, run_directory)
    retain = options.keep_sandbox == "always" or (
        options.keep_sandbox == "failure" and not accepted
    )
    if retain:
        (run_directory / "sandbox.txt").write_text(str(sandbox.root) + "\n", encoding="utf-8")
    else:
        sandbox.cleanup()
    return run_directory, int(ExitCode.ACCEPTED if accepted else ExitCode.DIFFERENCE)
