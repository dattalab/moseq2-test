from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from moseq2_test.provenance import sha256_file

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = ROOT / "environments"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def load(name: str) -> dict[str, object]:
    return yaml.safe_load((ENVIRONMENTS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("lock_name", "explicit_name", "expected_count"),
    [
        ("legacy-conda-linux-64.lock.yml", "legacy-conda-linux-64.explicit.txt", 55),
        (
            "legacy-container-runtime-linux-64.lock.yml",
            "legacy-container-runtime-linux-64.explicit.txt",
            13,
        ),
    ],
)
def test_conda_explicit_files_match_independent_hash_locks(
    lock_name: str, explicit_name: str, expected_count: int
) -> None:
    records = load(lock_name)["packages"]
    assert isinstance(records, list)
    lines = [
        line
        for line in (ENVIRONMENTS / explicit_name).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and line != "@EXPLICIT"
    ]
    assert len(records) == len(lines) == expected_count
    assert lines == [f"{item['url']}#{item['md5']}" for item in records]
    assert all(SHA256.fullmatch(str(item["sha256"])) for item in records)
    assert len({item["filename"] for item in records}) == expected_count


def test_pip_requirements_match_125_exact_python37_artifacts() -> None:
    records = load("legacy-pip-py37-linux-x86-64.lock.yml")["packages"]
    assert isinstance(records, list)
    lines = [
        line
        for line in (
            ENVIRONMENTS / "legacy-pip-py37-linux-x86-64.requirements.txt"
        ).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(records) == len(lines) == 125
    assert len({item["name"] for item in records}) == 125
    assert sum(bool(item["custom_built_wheel"]) for item in records) == 9
    for item, line in zip(records, lines, strict=True):
        assert line == (
            f"{item['name']} @ {item['install_url']} --hash=sha256:{item['sha256']}"
        )
        assert SHA256.fullmatch(str(item["sha256"]))
        assert str(item["install_url"]).startswith("https://")


def test_worker_input_lock_is_complete_and_content_addressed() -> None:
    records = load("legacy-worker-inputs.lock.yml")["objects"]
    assert isinstance(records, list)
    kinds: dict[str, int] = {}
    for item in records:
        kinds[str(item["kind"])] = kinds.get(str(item["kind"]), 0) + 1
        assert SHA256.fullmatch(str(item["sha256"]))
        assert not Path(str(item["local_source"])).is_absolute()
        if "moseq-data.s3.amazonaws.com" in str(item["canonical_url"]):
            assert f"/{item['sha256'][:2]}/{item['sha256']}/" in str(item["canonical_url"])
    assert kinds == {
        "baseline-wheel": 8,
        "custom-wheel": 9,
        "external-source": 1,
        "sdist": 8,
        "source-archive": 8,
        "test-tool-wheel": 6,
    }


def test_base_images_and_dockerfile_are_immutable() -> None:
    records = load("legacy-worker-base-images.lock.yml")["images"]
    assert isinstance(records, list) and len(records) == 3
    dockerfile = (ENVIRONMENTS / "legacy-worker.Dockerfile").read_text(encoding="utf-8")
    assert ":latest" not in dockerfile
    assert "apt-get" not in dockerfile
    for item in records:
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["index_digest"]))
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["platform_manifest_digest"]))
        assert f"{item['tag']}@{item['index_digest']}" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "micromamba create --yes --offline" in dockerfile


def test_entrypoint_builds_and_installs_the_pinned_action_checkout_offline() -> None:
    entrypoint = (ENVIRONMENTS / "legacy-worker-entrypoint.sh").read_text(encoding="utf-8")
    assert "uv build --offline --wheel" in entrypoint
    assert "uv pip install --offline --reinstall --no-deps" in entrypoint
    assert "MOSEQ2_TEST_ACTION_ROOT" in entrypoint


def test_test_tool_wheels_match_the_locked_chunk0_files() -> None:
    records = load("legacy-test-tools.lock.yml")["packages"]
    assert isinstance(records, list) and len(records) == 6
    legacy = Path(
        "/n/groups/datta/john/projects/user-support/2026-08-02-moseq2-modernization/"
        "environments/2026-08-02_moseq2_modernization/test_tool_wheels"
    )
    if not legacy.is_dir():
        pytest.skip("Chunk 0 evidence is not mounted")
    for item in records:
        path = legacy / str(item["filename"])
        assert path.stat().st_size == item["size"]
        assert sha256_file(path) == item["sha256"]
