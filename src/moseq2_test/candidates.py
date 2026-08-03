"""Source export, isolated wheel builds, and candidate integrity checks."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import yaml
from packaging.utils import canonicalize_name, parse_wheel_filename
from pydantic import ValidationError

from moseq2_test.config import load_yaml, resource
from moseq2_test.errors import InvalidConfiguration, MissingInput
from moseq2_test.models import CandidateKind, CandidateRecord, CandidateSet
from moseq2_test.provenance import sha256_file
from moseq2_test.sandbox import Sandbox

PACKAGE_ALIASES = {
    "autoregressive": "pyhsmm-autoregressive",
    "pyhsmm-autoregressive": "pyhsmm-autoregressive",
}
TARGET_PACKAGES = {
    "moseq2-extract",
    "moseq2-pca",
    "moseq2-model",
    "moseq2-viz",
    "moseq2-app",
    "pybasicbayes",
    "pyhsmm",
    "pyhsmm-autoregressive",
}
EIGEN_PACKAGES = {"pyhsmm", "pyhsmm-autoregressive"}
EIGEN_MAX_MEMBERS = 5_000
EIGEN_MAX_UNPACKED_BYTES = 100 * 1024 * 1024
BUILD_TOOLCHAIN_ENVIRONMENT = "MOSEQ2_TEST_BUILD_TOOLCHAIN_PREFIX"


def canonical_package(name: str) -> str:
    canonical = canonicalize_name(name)
    return PACKAGE_ALIASES.get(canonical, canonical)


def parse_assignment(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise InvalidConfiguration(f"expected NAME=PATH, received {value!r}")
    raw_name, raw_path = value.split("=", 1)
    name = canonical_package(raw_name)
    if name not in TARGET_PACKAGES:
        raise InvalidConfiguration(f"unknown MoSeq2 target package: {raw_name}")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise MissingInput(f"candidate path does not exist: {path}")
    return name, path


def _git_value(source: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise InvalidConfiguration(f"not a readable Git checkout: {source}")
    return completed.stdout.strip()


def export_source(source: Path, destination: Path, *, allow_dirty: bool) -> tuple[str, bool]:
    commit = _git_value(source, "rev-parse", "HEAD")
    status = _git_value(source, "status", "--porcelain", "--untracked-files=all")
    dirty = bool(status)
    if dirty and not allow_dirty:
        raise InvalidConfiguration(f"source checkout is dirty: {source}")
    if destination.exists():
        raise InvalidConfiguration(f"source export destination already exists: {destination}")
    destination.mkdir(parents=True)
    if dirty:
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "dist", "build", "*.egg-info", ".tox", ".venv"),
        )
    else:
        completed = subprocess.run(
            ["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
            check=True,
            capture_output=True,
        )
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            archive.extractall(destination, filter="data")
    return commit, dirty


def _locked_eigen_record() -> dict[str, object]:
    with resource("environments", "external-sources.lock.yml") as lock_path:
        records = load_yaml(lock_path).get("sources", [])
    matches = [
        item for item in records if isinstance(item, dict) and item.get("id") == "eigen-3.3.7"
    ]
    if len(matches) != 1:
        raise InvalidConfiguration("external source lock must contain exactly one Eigen 3.3.7")
    record = matches[0]
    if not (
        isinstance(record.get("filename"), str)
        and isinstance(record.get("size"), int)
        and isinstance(record.get("sha256"), str)
    ):
        raise InvalidConfiguration("Eigen 3.3.7 external source lock is malformed")
    return record


def _stage_locked_eigen(package: str, export: Path) -> dict[str, object] | None:
    """Stage the exact locked Eigen headers required by two historical builds."""
    if package not in EIGEN_PACKAGES:
        return None
    dependency_root = export / "deps"
    destination = dependency_root / "Eigen"
    if dependency_root.is_symlink() or destination.is_symlink():
        raise InvalidConfiguration("candidate Eigen path may not be a symbolic link")
    if destination.exists():
        if not destination.is_dir() or any(path.is_symlink() for path in destination.rglob("*")):
            raise InvalidConfiguration("candidate Eigen input must be a regular directory tree")
        return {"source": "candidate", "destination": "deps/Eigen"}

    record = _locked_eigen_record()
    mirror_value = os.environ.get("MOSEQ2_TEST_EXTERNAL_SOURCE_MIRROR")
    if not mirror_value:
        raise MissingInput("MOSEQ2_TEST_EXTERNAL_SOURCE_MIRROR is required to stage Eigen")
    archive_path = Path(mirror_value).expanduser().resolve() / str(record["filename"])
    if not archive_path.is_file():
        raise MissingInput(f"locked Eigen archive is missing: {archive_path}")
    actual_size = archive_path.stat().st_size
    if actual_size != record["size"]:
        raise MissingInput(
            f"locked Eigen archive has size {actual_size}; expected {record['size']}"
        )
    actual_hash = sha256_file(archive_path)
    if actual_hash != record["sha256"]:
        raise MissingInput(
            f"locked Eigen archive has SHA-256 {actual_hash}; expected {record['sha256']}"
        )

    destination.mkdir(parents=True)
    try:
        destination.resolve().relative_to(export.resolve())
    except ValueError as error:
        raise InvalidConfiguration("Eigen destination escapes the source export") from error
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        if len(members) > EIGEN_MAX_MEMBERS:
            raise InvalidConfiguration("locked Eigen archive exceeds its member ceiling")
        if sum(member.size for member in members) > EIGEN_MAX_UNPACKED_BYTES:
            raise InvalidConfiguration("locked Eigen archive exceeds its size ceiling")
        copied = 0
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise InvalidConfiguration(f"unsafe Eigen archive member: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise InvalidConfiguration(
                    f"unsupported Eigen archive member type: {member.name}"
                )
            if relative.parts[:2] != ("eigen-3.3.7", "Eigen"):
                continue
            eigen_relative = relative.parts[2:]
            if not eigen_relative:
                continue
            target = destination.joinpath(*eigen_relative)
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as error:
                raise InvalidConfiguration(
                    f"Eigen archive member escapes destination: {member.name}"
                ) from error
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise InvalidConfiguration(f"cannot read Eigen archive member {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            copied += 1
    if copied == 0 or not (destination / "Core").is_file():
        raise InvalidConfiguration("locked Eigen archive did not provide the required headers")
    return {
        "source": "locked-external-source",
        "id": "eigen-3.3.7",
        "filename": record["filename"],
        "size": record["size"],
        "sha256": record["sha256"],
        "destination": "deps/Eigen",
    }


def inspect_wheel(path: Path, *, expected_package: str | None = None) -> dict[str, object]:
    if not path.is_file() or path.suffix != ".whl":
        raise MissingInput(f"candidate is not one concrete wheel: {path}")
    try:
        distribution, version, _build, tags = parse_wheel_filename(path.name)
    except ValueError as error:
        raise InvalidConfiguration(f"invalid wheel filename {path.name}: {error}") from error
    package = canonical_package(str(distribution))
    if expected_package is not None and package != canonical_package(expected_package):
        raise InvalidConfiguration(
            f"wheel {path.name} provides {package}, expected {canonical_package(expected_package)}"
        )
    direct_urls: list[dict[str, object]] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any(name.endswith(".egg-link") for name in names):
            raise InvalidConfiguration(f"wheel contains an editable egg-link: {path}")
        for name in names:
            if name.endswith(".dist-info/direct_url.json"):
                value = json.loads(archive.read(name))
                direct_urls.append(value)
                if value.get("dir_info", {}).get("editable") is True:
                    raise InvalidConfiguration(f"wheel records an editable installation: {path}")
    tag_values = sorted(str(tag) for tag in tags)
    return {
        "package": package,
        "version": str(version),
        "filename": path.name,
        "sha256": sha256_file(path),
        "tags": tag_values,
        "direct_urls": direct_urls,
    }


def _candidate_build_environment() -> tuple[dict[str, str], dict[str, object] | None]:
    """Select the separately locked compiler prefix when the worker declares it."""
    environment = os.environ.copy()
    raw_prefix = environment.get(BUILD_TOOLCHAIN_ENVIRONMENT)
    if not raw_prefix:
        return environment, None
    prefix = Path(raw_prefix).expanduser()
    if not prefix.is_absolute():
        raise InvalidConfiguration(f"{BUILD_TOOLCHAIN_ENVIRONMENT} must be absolute")
    prefix = prefix.resolve()
    if not prefix.is_dir():
        raise MissingInput(f"candidate build toolchain prefix is missing: {prefix}")
    with resource("environments", "legacy-build-toolchain-linux-64.lock.yml") as lock_path:
        lock = load_yaml(lock_path)
    compiler = lock.get("compiler")
    if not isinstance(compiler, dict):
        raise InvalidConfiguration("candidate build toolchain lock has no compiler record")
    cc_name = compiler.get("cc")
    cxx_name = compiler.get("cxx")
    if not isinstance(cc_name, str) or not isinstance(cxx_name, str):
        raise InvalidConfiguration("candidate build toolchain compiler names are malformed")
    bin_directory = prefix / "bin"
    cc = bin_directory / cc_name
    cxx = bin_directory / cxx_name
    for label, path in (("C compiler", cc), ("C++ compiler", cxx)):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise MissingInput(f"locked {label} is missing or not executable: {path}")
    inherited_path = environment.get("PATH")
    environment["PATH"] = str(bin_directory) + (
        f"{os.pathsep}{inherited_path}" if inherited_path else ""
    )
    environment["CC"] = str(cc)
    environment["CXX"] = str(cxx)
    locked_environment = lock.get("build_environment")
    if not isinstance(locked_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in locked_environment.items()
    ):
        raise InvalidConfiguration("candidate build toolchain environment is malformed")
    selected_environment: dict[str, str] = {}
    for key, value in locked_environment.items():
        expanded = value.format(prefix=prefix)
        environment[key] = expanded
        selected_environment[key] = expanded
    return environment, {
        "lock_id": lock.get("lock_id"),
        "prefix": str(prefix),
        "version": compiler.get("version"),
        "cc": str(cc),
        "cxx": str(cxx),
        "environment": selected_environment,
    }


def _build_distributions(
    *,
    python: Path,
    source: Path,
    output: Path,
    legacy_setup: bool,
    environment: dict[str, str] | None = None,
) -> list[subprocess.CompletedProcess[str]]:
    """Build an sdist and wheel without importing the candidate in the controller.

    The historical repositories predate PEP 517 and expose ``setup.py``.  Their
    Python 3.7 target intentionally does not carry the modern ``build`` package,
    so an explicitly selected target interpreter uses the two native legacy
    build commands.  Controller-native builds keep the isolated PEP 517 path.
    """
    if legacy_setup:
        commands = [
            [str(python), "setup.py", "sdist", "--dist-dir", str(output)],
            [
                str(python),
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(output),
                ".",
            ],
        ]
    else:
        commands = [
            [
                str(python),
                "-m",
                "build",
                "--sdist",
                "--wheel",
                "--outdir",
                str(output),
            ]
        ]
    if environment is None:
        environment, _toolchain = _candidate_build_environment()
    completed: list[subprocess.CompletedProcess[str]] = []
    for command in commands:
        result = subprocess.run(
            command,
            cwd=source,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        completed.append(result)
        if result.returncode != 0:
            break
    return completed


def build_sources(
    assignments: list[str],
    *,
    workspace: Path,
    output: Path,
    allow_dirty: bool,
    build_python: Path | None = None,
) -> CandidateSet:
    if not assignments:
        raise InvalidConfiguration("provide at least one --source NAME=PATH")
    parsed = [parse_assignment(value) for value in assignments]
    names = [name for name, _ in parsed]
    if len(names) != len(set(names)):
        raise InvalidConfiguration("duplicate source candidate package")
    sandbox = Sandbox.create(workspace, prefix="moseq2-test-candidates-")
    output.mkdir(parents=True, exist_ok=True)
    (output / "logs").mkdir(exist_ok=True)
    (output / "test-sources").mkdir(exist_ok=True)
    candidates: list[CandidateRecord] = []
    success = False
    try:
        for name, source in parsed:
            export = sandbox.sources / name
            commit, dirty = export_source(source, export, allow_dirty=allow_dirty)
            external_build_input = _stage_locked_eigen(name, export)
            wheel_output = sandbox.wheelhouse / name
            wheel_output.mkdir()
            python = build_python or Path(sys.executable)
            if not python.is_file():
                raise MissingInput(f"candidate build Python does not exist: {python}")
            build_environment, build_toolchain = _candidate_build_environment()
            completed = _build_distributions(
                python=python,
                source=export,
                output=wheel_output,
                legacy_setup=build_python is not None and (export / "setup.py").is_file(),
                environment=build_environment,
            )
            stdout = "\n".join(
                f"$ {' '.join(result.args)}\n{result.stdout}" for result in completed
            )
            if external_build_input is not None:
                stdout = (
                    "$ moseq2-test stage locked external build input\n"
                    + json.dumps(external_build_input, sort_keys=True)
                    + "\n"
                    + stdout
                )
            if build_toolchain is not None:
                stdout = (
                    "$ moseq2-test select locked candidate build toolchain\n"
                    + json.dumps(build_toolchain, sort_keys=True)
                    + "\n"
                    + stdout
                )
            stderr = "\n".join(
                f"$ {' '.join(result.args)}\n{result.stderr}" for result in completed
            )
            (sandbox.result / f"{name}-build.stdout.log").write_text(
                stdout, encoding="utf-8"
            )
            (sandbox.result / f"{name}-build.stderr.log").write_text(
                stderr, encoding="utf-8"
            )
            shutil.copy2(
                sandbox.result / f"{name}-build.stdout.log",
                output / "logs" / f"{name}-build.stdout.log",
            )
            shutil.copy2(
                sandbox.result / f"{name}-build.stderr.log",
                output / "logs" / f"{name}-build.stderr.log",
            )
            if not completed or completed[-1].returncode != 0:
                raise InvalidConfiguration(
                    f"wheel build failed for {name} with {python}; see sandbox {sandbox.root}"
                )
            wheels = list(wheel_output.glob("*.whl"))
            if len(wheels) != 1:
                raise InvalidConfiguration(f"expected one wheel for {name}, found {len(wheels)}")
            details = inspect_wheel(wheels[0], expected_package=name)
            final = output / wheels[0].name
            if final.exists() and sha256_file(final) != details["sha256"]:
                raise InvalidConfiguration(f"refusing to overwrite different wheel: {final}")
            shutil.copy2(wheels[0], final)
            sdists = [
                path for path in wheel_output.iterdir() if path.is_file() and path.suffix != ".whl"
            ]
            if len(sdists) != 1:
                raise InvalidConfiguration(f"expected one sdist for {name}, found {len(sdists)}")
            final_sdist = output / sdists[0].name
            if final_sdist.exists() and sha256_file(final_sdist) != sha256_file(sdists[0]):
                raise InvalidConfiguration(f"refusing to overwrite different sdist: {final_sdist}")
            shutil.copy2(sdists[0], final_sdist)
            test_snapshot = output / "test-sources" / name
            shutil.copytree(export, test_snapshot)
            candidates.append(
                CandidateRecord(
                    package=name,
                    kind=CandidateKind.WHEEL,
                    location=str(final.resolve()),
                    sha256=str(details["sha256"]),
                    source_commit=commit,
                    test_source=str(test_snapshot.resolve()),
                    sdist_location=str(final_sdist.resolve()),
                    sdist_sha256=sha256_file(final_sdist),
                    dirty=dirty,
                )
            )
        result = CandidateSet(candidates=candidates)
        (output / "candidates.json").write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        success = True
        return result
    finally:
        if success:
            sandbox.cleanup()


def load_candidate_set(path: Path) -> CandidateSet:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return CandidateSet.model_validate(value)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise InvalidConfiguration(f"invalid candidate set {path}: {error}") from error


def verify_candidate_set(candidate_set: CandidateSet, *, base: Path) -> None:
    for candidate in candidate_set.candidates:
        location = Path(candidate.location)
        if not location.is_absolute():
            location = (base / location).resolve()
        if candidate.kind == CandidateKind.WHEEL:
            details = inspect_wheel(location, expected_package=candidate.package)
            if candidate.sha256 and details["sha256"] != candidate.sha256:
                raise InvalidConfiguration(
                    f"candidate {candidate.package} hash differs from its manifest"
                )


def verify_installed_locations(response: dict[str, object], *, forbidden_roots: list[Path]) -> None:
    records = response.get("imports")
    if not isinstance(records, list):
        raise InvalidConfiguration("worker installation response has no import records")
    roots = [path.resolve() for path in forbidden_roots]
    for raw_record in records:
        if not isinstance(raw_record, dict) or not raw_record.get("imported"):
            raise InvalidConfiguration(f"target import failed: {raw_record}")
        raw_path = raw_record.get("file")
        if not isinstance(raw_path, str):
            raise InvalidConfiguration(f"target import has no file location: {raw_record}")
        import_path = Path(raw_path).resolve()
        for root in roots:
            if import_path == root or root in import_path.parents:
                raise InvalidConfiguration(
                    f"target import escaped to forbidden root: {import_path}"
                )
    distributions = response.get("distributions")
    if not isinstance(distributions, list):
        raise InvalidConfiguration("worker response has no distribution records")
    for raw_record in distributions:
        if not isinstance(raw_record, dict):
            raise InvalidConfiguration("malformed distribution record")
        direct_url = raw_record.get("direct_url")
        if isinstance(direct_url, dict) and direct_url.get("dir_info", {}).get("editable") is True:
            raise InvalidConfiguration(f"editable target distribution: {raw_record.get('name')}")
