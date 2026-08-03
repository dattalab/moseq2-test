"""Immutable fixture registry, content-addressed cache, and safe publication."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import urlsplit

import requests
from filelock import FileLock

from moseq2_test.errors import InvalidConfiguration, MissingInput, Moseq2TestError
from moseq2_test.models import FixtureManifest, FixtureObject
from moseq2_test.provenance import redact_url, sha256_file
from moseq2_test.registry import fixture_manifest, profile

BLOCK_SIZE = 8 * 1024 * 1024
S3_BUCKET = "moseq-data"


@dataclass(frozen=True)
class CacheLayout:
    root: Path

    @property
    def objects(self) -> Path:
        return self.root / "objects" / "sha256"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata"

    @property
    def extracted(self) -> Path:
        return self.root / "extracted" / "sha256"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    @property
    def temporary(self) -> Path:
        return self.root / "tmp"

    def prepare(self) -> None:
        for path in (self.objects, self.metadata, self.extracted, self.locks, self.temporary):
            path.mkdir(parents=True, exist_ok=True)

    def object_path(self, sha256: str) -> Path:
        return self.objects / sha256[:2] / sha256

    def metadata_path(self, sha256: str) -> Path:
        return self.metadata / sha256[:2] / f"{sha256}.json"

    def lock_path(self, sha256: str) -> Path:
        return self.locks / f"{sha256}.lock"


def _selected_manifests(
    *, profile_name: str | None, fixture_sets: list[str]
) -> list[FixtureManifest]:
    names = list(fixture_sets)
    if profile_name:
        selected = profile(profile_name, require_implemented=False)
        names.extend(selected.fixture_sets)
    names = list(dict.fromkeys(names))
    if not names:
        raise InvalidConfiguration("select --profile or at least one --fixture-set")
    return [fixture_manifest(name) for name in names]


def _unique_objects(manifests: Iterable[FixtureManifest]) -> list[FixtureObject]:
    by_hash: dict[str, FixtureObject] = {}
    for manifest in manifests:
        for item in manifest.objects:
            previous = by_hash.get(item.sha256)
            if previous is not None and previous.size != item.size:
                raise InvalidConfiguration(f"conflicting metadata for {item.sha256}")
            by_hash[item.sha256] = item
    return sorted(by_hash.values(), key=lambda item: item.id)


def verify_object(path: Path, item: FixtureObject) -> None:
    if not path.is_file():
        raise MissingInput(f"missing fixture {item.id} ({item.sha256})")
    actual_size = path.stat().st_size
    if actual_size != item.size:
        raise MissingInput(f"fixture {item.id} has size {actual_size}; expected {item.size}")
    actual_hash = sha256_file(path)
    if actual_hash != item.sha256:
        raise MissingInput(f"fixture {item.id} has SHA-256 {actual_hash}; expected {item.sha256}")


def _mirror_candidates(mirror: Path, item: FixtureObject) -> list[Path]:
    direct = [
        mirror / "objects" / "sha256" / item.sha256[:2] / item.sha256,
        mirror / item.sha256,
        mirror / item.filename,
    ]
    recursive = sorted(
        path for path in mirror.rglob(item.filename) if path.is_file() and path not in direct
    )
    return [*direct, *recursive]


def _copy_verified(source: Path, temporary: Path, item: FixtureObject) -> None:
    verify_object(source, item)
    with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=BLOCK_SIZE)
        output_stream.flush()
        os.fsync(output_stream.fileno())


def _download(url: str, temporary: Path, item: FixtureObject, retries: int = 3) -> None:
    for attempt in range(retries):
        offset = temporary.stat().st_size if temporary.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 120)) as response:
                if offset and response.status_code != 206:
                    temporary.unlink(missing_ok=True)
                    offset = 0
                    response.close()
                    return _download(url, temporary, item, retries - attempt)
                response.raise_for_status()
                mode = "ab" if offset else "wb"
                with temporary.open(mode) as stream:
                    for block in response.iter_content(BLOCK_SIZE):
                        if block:
                            stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
            verify_object(temporary, item)
            return
        except (OSError, requests.RequestException, MissingInput) as error:
            if attempt + 1 == retries:
                raise MissingInput(
                    f"failed to fetch {item.id} from {redact_url(url)}: {error}"
                ) from error
            time.sleep(2**attempt)


def fetch_object(
    layout: CacheLayout,
    item: FixtureObject,
    *,
    mirror: Path | None = None,
    offline: bool = False,
) -> Path:
    layout.prepare()
    destination = layout.object_path(item.sha256)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(layout.lock_path(item.sha256))):
        if destination.exists():
            try:
                verify_object(destination, item)
                return destination
            except MissingInput:
                destination.unlink()
        temporary = Path(
            tempfile.mkstemp(prefix=f"{item.sha256}.", suffix=".partial", dir=layout.temporary)[1]
        )
        try:
            copied = False
            if mirror is not None:
                for candidate in _mirror_candidates(mirror, item):
                    if candidate.is_file():
                        _copy_verified(candidate, temporary, item)
                        copied = True
                        break
            if not copied:
                if offline:
                    raise MissingInput(f"offline cache is missing fixture {item.id}")
                errors: list[str] = []
                for url in [str(value) for value in item.canonical_urls] + item.provenance_urls:
                    try:
                        _download(url, temporary, item)
                        copied = True
                        break
                    except MissingInput as error:
                        errors.append(str(error))
                if not copied:
                    raise MissingInput("; ".join(errors))
            verify_object(temporary, item)
            os.chmod(temporary, 0o444)
            os.replace(temporary, destination)
            metadata = {
                "schema_version": 1,
                "id": item.id,
                "size": item.size,
                "sha256": item.sha256,
                "verified_at": time.time(),
            }
            metadata_path = layout.metadata_path(item.sha256)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            pending_metadata = metadata_path.with_suffix(f".json.{os.getpid()}.tmp")
            pending_metadata.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(pending_metadata, metadata_path)
            return destination
        finally:
            temporary.unlink(missing_ok=True)


def fetch_selected(
    cache_dir: Path,
    *,
    profile_name: str | None,
    fixture_sets: list[str],
    mirror: Path | None,
    offline: bool,
) -> dict[str, Any]:
    manifests = _selected_manifests(profile_name=profile_name, fixture_sets=fixture_sets)
    objects = _unique_objects(manifests)
    layout = CacheLayout(cache_dir)
    paths = [fetch_object(layout, item, mirror=mirror, offline=offline) for item in objects]
    return {
        "status": "verified",
        "fixture_sets": [item.fixture_set for item in manifests],
        "objects": len(paths),
        "bytes": sum(path.stat().st_size for path in paths),
        "cache": str(layout.root.resolve()),
    }


def verify_selected(
    cache_dir: Path, *, profile_name: str | None, fixture_sets: list[str]
) -> dict[str, Any]:
    manifests = _selected_manifests(profile_name=profile_name, fixture_sets=fixture_sets)
    objects = _unique_objects(manifests)
    layout = CacheLayout(cache_dir)
    missing: list[str] = []
    invalid: list[str] = []
    for item in objects:
        path = layout.object_path(item.sha256)
        if not path.is_file():
            missing.append(item.id)
            continue
        try:
            verify_object(path, item)
        except MissingInput as error:
            invalid.append(str(error))
    if missing or invalid:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if invalid:
            details.append("invalid: " + "; ".join(invalid))
        raise MissingInput(" | ".join(details))
    return {
        "status": "verified",
        "fixture_sets": [item.fixture_set for item in manifests],
        "objects": len(objects),
        "bytes": sum(item.size for item in objects),
    }


def _safe_relative(name: str, *, allow_root_marker: bool = False) -> Path | None:
    if allow_root_marker and name == "/":
        return None
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise InvalidConfiguration(f"unsafe archive member: {name!r}")
    return Path(*pure.parts)


def _within(root: Path, destination: Path) -> None:
    try:
        destination.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise InvalidConfiguration(f"archive destination escapes root: {destination}") from error


def _copy_member(source: IO[bytes], destination: Path, root: Path) -> None:
    _within(root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        shutil.copyfileobj(source, output, length=BLOCK_SIZE)


def _safe_extract_zip(archive_path: Path, root: Path, item: FixtureObject) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        total = sum(member.file_size for member in members)
        if item.max_members is not None and len(members) > item.max_members:
            raise InvalidConfiguration(f"archive {item.id} exceeds its member ceiling")
        if item.max_unpacked_bytes is not None and total > item.max_unpacked_bytes:
            raise InvalidConfiguration(f"archive {item.id} exceeds its size ceiling")
        for member in members:
            relative = _safe_relative(member.filename, allow_root_marker=True)
            if relative is None:
                continue
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or not (member.is_dir() or file_type in {0, stat.S_IFREG}):
                raise InvalidConfiguration(f"unsupported ZIP member type: {member.filename}")
            destination = root / relative
            if member.is_dir():
                _within(root, destination)
                destination.mkdir(parents=True, exist_ok=True)
                continue
            with archive.open(member) as source:
                _copy_member(source, destination, root)


def _safe_extract_tar(archive_path: Path, root: Path, item: FixtureObject) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        total = sum(member.size for member in members)
        if item.max_members is not None and len(members) > item.max_members:
            raise InvalidConfiguration(f"archive {item.id} exceeds its member ceiling")
        if item.max_unpacked_bytes is not None and total > item.max_unpacked_bytes:
            raise InvalidConfiguration(f"archive {item.id} exceeds its size ceiling")
        for member in members:
            relative = _safe_relative(member.name)
            assert relative is not None
            destination = root / relative
            if member.isdir():
                _within(root, destination)
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise InvalidConfiguration(f"cannot read archive member {member.name}")
                with source:
                    _copy_member(source, destination, root)
            else:
                raise InvalidConfiguration(f"unsupported TAR member type: {member.name}")


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def extract_object(layout: CacheLayout, item: FixtureObject, *, recipe_version: str = "1") -> Path:
    if item.unpack == "none":
        return layout.object_path(item.sha256)
    source = layout.object_path(item.sha256)
    verify_object(source, item)
    destination = layout.extracted / item.sha256 / recipe_version
    with FileLock(str(layout.lock_path(f"extract-{item.sha256}-{recipe_version}"))):
        marker = destination / ".moseq2-test-extracted.json"
        if marker.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{item.sha256}.", dir=layout.temporary))
        try:
            if item.unpack == "zip":
                _safe_extract_zip(source, temporary, item)
            elif item.unpack in {"tar", "tar.gz"}:
                _safe_extract_tar(source, temporary, item)
            else:
                raise InvalidConfiguration(f"unknown archive type {item.unpack}")
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(temporary, destination)
            marker = destination / ".moseq2-test-extracted.json"
            marker.write_text(
                json.dumps({"sha256": item.sha256, "recipe_version": recipe_version}) + "\n",
                encoding="utf-8",
            )
            _make_read_only(destination)
            return destination
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _object_key(item: FixtureObject) -> str:
    url = urlsplit(str(item.canonical_urls[0]))
    return url.path.lstrip("/")


def _find_source(source_root: Path, item: FixtureObject) -> Path:
    candidates = [path for path in source_root.rglob(item.filename) if path.is_file()]
    for candidate in candidates:
        if candidate.stat().st_size == item.size and sha256_file(candidate) == item.sha256:
            return candidate
    raise MissingInput(f"cannot locate source bytes for {item.id} below {source_root}")


def _anonymous_verify(item: FixtureObject) -> None:
    temporary = Path(tempfile.mkstemp(prefix="moseq2-test-anonymous-")[1])
    try:
        _download(str(item.canonical_urls[0]), temporary, item)
    finally:
        temporary.unlink(missing_ok=True)


def publish_fixture_set(
    name: str,
    *,
    source_root: Path,
    cache_dir: Path,
    dry_run: bool = True,
) -> dict[str, Any]:
    del cache_dir
    manifest = fixture_manifest(name)
    actions: list[dict[str, Any]] = []
    sources: dict[str, Path] = {}
    for item in manifest.objects:
        source = _find_source(source_root, item)
        sources[item.id] = source
        actions.append(
            {
                "id": item.id,
                "bucket": S3_BUCKET,
                "key": _object_key(item),
                "source": str(source),
                "size": item.size,
                "sha256": item.sha256,
                "mode": "create-only-public-read",
            }
        )
    if dry_run:
        return {"status": "dry-run", "fixture_set": name, "actions": actions}
    try:
        import boto3  # type: ignore[import-untyped]
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]
    except ImportError as error:  # pragma: no cover - installation error
        raise InvalidConfiguration("install moseq2-test[publish] to publish fixtures") from error
    client = boto3.client("s3", region_name="us-east-1")
    completed: list[str] = []
    for item in manifest.objects:
        key = _object_key(item)
        source = sources[item.id]
        try:
            with source.open("rb") as stream:
                client.put_object(
                    Bucket=S3_BUCKET,
                    Key=key,
                    Body=stream,
                    ContentLength=item.size,
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(item.sha256)).decode("ascii"),
                    ServerSideEncryption="AES256",
                    ACL="public-read",
                    IfNoneMatch="*",
                    Metadata={
                        "moseq2-test-id": item.id,
                        "sha256": item.sha256,
                        "license-id": item.license_id,
                        "trust": str(item.trust),
                    },
                )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "412"}:
                raise InvalidConfiguration(
                    f"refusing to overwrite s3://{S3_BUCKET}/{key}"
                ) from error
            raise Moseq2TestError(f"S3 upload failed for {item.id}: {code}") from error
        _anonymous_verify(item)
        completed.append(item.id)
    return {"status": "published", "fixture_set": name, "objects": completed}
