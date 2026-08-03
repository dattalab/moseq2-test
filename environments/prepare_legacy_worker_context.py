#!/usr/bin/env python3
"""Fetch and verify every immutable input used to build the legacy-worker image."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import yaml

BLOCK_SIZE = 8 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_download(url: str, destination: Path, *, size: int, sha256: str) -> Path:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise RuntimeError(f"worker input URL must be anonymous HTTPS: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if destination.stat().st_size == size and sha256_file(destination) == sha256:
            return destination
        raise RuntimeError(f"existing worker input is invalid: {destination}")
    descriptor, pending_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    pending = Path(pending_name)
    try:
        with open(descriptor, "wb") as output, requests.get(
            url, stream=True, timeout=(15, 180), allow_redirects=True
        ) as response:
            response.raise_for_status()
            if urlsplit(response.url).scheme != "https":
                raise RuntimeError(f"worker input redirected away from HTTPS: {url}")
            for block in response.iter_content(BLOCK_SIZE):
                if block:
                    output.write(block)
        if pending.stat().st_size != size:
            raise RuntimeError(
                f"worker input {destination.name} has {pending.stat().st_size} bytes; "
                f"expected {size}"
            )
        actual = sha256_file(pending)
        if actual != sha256:
            raise RuntimeError(
                f"worker input {destination.name} has SHA-256 {actual}; expected {sha256}"
            )
        pending.replace(destination)
        return destination
    finally:
        pending.unlink(missing_ok=True)


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def fetch_record(record: dict[str, Any], directory: Path, url_key: str = "url") -> Path:
    return verified_download(
        str(record[url_key]),
        directory / str(record["filename"]),
        size=int(record["size"]),
        sha256=str(record["sha256"]),
    )


def prepare(repository: Path, output: Path) -> dict[str, Any]:
    repository = repository.resolve()
    if output.exists():
        if any(output.iterdir()):
            raise RuntimeError(f"refusing to reuse nonempty worker context: {output}")
    else:
        output.mkdir(parents=True)

    environments = repository / "environments"
    conda = load(environments / "legacy-conda-linux-64.lock.yml")
    runtime = load(environments / "legacy-container-runtime-linux-64.lock.yml")
    build_toolchain = load(environments / "legacy-build-toolchain-linux-64.lock.yml")
    pip_lock = load(environments / "legacy-pip-py37-linux-x86-64.lock.yml")
    test_tools = load(environments / "legacy-test-tools.lock.yml")
    worker = load(environments / "legacy-worker-inputs.lock.yml")

    download_jobs: list[tuple[dict[str, Any], Path, str]] = []
    for record in [
        *conda["packages"],
        *runtime["packages"],
        *build_toolchain["packages"],
    ]:
        download_jobs.append((record, output / "conda", "url"))
    for record in pip_lock["packages"]:
        selected = {
            "url": record["install_url"],
            "filename": record["filename"],
            "size": record["size"],
            "sha256": record["sha256"],
        }
        download_jobs.append((selected, output / "pip", "url"))
    kind_directories = {
        "source-archive": "source_archives",
        "sdist": "sdists",
        "baseline-wheel": "wheels",
        "custom-wheel": "pip",
        "test-tool-wheel": "test_tool_wheels",
        "external-source": "external_sources",
    }
    for record in worker["objects"]:
        directory = output / kind_directories[str(record["kind"])]
        download_jobs.append((record, directory, "canonical_url"))

    unique: dict[tuple[Path, str], tuple[dict[str, Any], Path, str]] = {}
    for record, directory, url_key in download_jobs:
        key = (directory, str(record["filename"]))
        previous = unique.get(key)
        if previous is not None and previous[0]["sha256"] != record["sha256"]:
            raise RuntimeError(f"conflicting worker inputs for {key[1]}")
        unique[key] = (record, directory, url_key)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: fetch_record(item[0], item[1], item[2]), unique.values()))

    conda_explicit = [
        "# Local, hash-verified baseline and container-only runtime packages.",
        "@EXPLICIT",
    ]
    for record in [*conda["packages"], *runtime["packages"]]:
        conda_explicit.append(
            f"file:///tmp/worker-inputs/conda/{record['filename']}#{record['md5']}"
        )
    (output / "conda-local.explicit.txt").write_text(
        "\n".join(conda_explicit) + "\n", encoding="utf-8"
    )

    build_toolchain_explicit = [
        "# Local, hash-verified candidate build toolchain packages.",
        "@EXPLICIT",
    ]
    for record in build_toolchain["packages"]:
        build_toolchain_explicit.append(
            f"file:///tmp/worker-inputs/conda/{record['filename']}#{record['md5']}"
        )
    (output / "build-toolchain-local.explicit.txt").write_text(
        "\n".join(build_toolchain_explicit) + "\n", encoding="utf-8"
    )

    pip_requirements = ["# Hash-locked Python 3.7 dependencies staged in the build context."]
    for record in pip_lock["packages"]:
        pip_requirements.append(
            f"{record['name']} @ file:///tmp/worker-inputs/pip/{record['filename']} "
            f"--hash=sha256:{record['sha256']}"
        )
    (output / "pip-local.requirements.txt").write_text(
        "\n".join(pip_requirements) + "\n", encoding="utf-8"
    )

    baseline_requirements = ["# The eight exact MoSeq2 baseline wheels."]
    for record in worker["objects"]:
        if record["kind"] == "baseline-wheel":
            baseline_requirements.append(
                f"file:///tmp/worker-inputs/wheels/{record['filename']} "
                f"--hash=sha256:{record['sha256']}"
            )
    (output / "baseline-wheels-local.requirements.txt").write_text(
        "\n".join(baseline_requirements) + "\n", encoding="utf-8"
    )

    metadata = output / "metadata"
    metadata.mkdir()
    for path in sorted(environments.glob("*.lock.yml")):
        shutil.copy2(path, metadata / path.name)
    for name in ("LICENSE.md", "NOTICE.md"):
        shutil.copy2(repository / name, metadata / name)
    shutil.copytree(repository / "licenses", metadata / "licenses")

    manifest = {
        "schema_version": 1,
        "files": [
            {
                "path": str(path.relative_to(output)),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.name != "context-manifest.json"
        ],
    }
    manifest["bytes"] = sum(item["size"] for item in manifest["files"])
    (output / "context-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result = {
        "files": len(manifest["files"]),
        "bytes": manifest["bytes"],
        "sha256": sha256_file(output / "context-manifest.json"),
        "test_tools": len(test_tools["packages"]),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.repository, args.output.resolve())


if __name__ == "__main__":
    main()
