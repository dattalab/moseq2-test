from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _documents() -> list[tuple[Path, dict[str, object]]]:
    paths = [ROOT / "action.yml", *sorted((ROOT / ".github/workflows").glob("*.yml"))]
    return [(path, yaml.safe_load(path.read_text(encoding="utf-8"))) for path in paths]


def _uses_values(value: object) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, child in value.items():
            if key == "uses" and isinstance(child, str):
                result.append(child)
            result.extend(_uses_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_uses_values(child))
        return result
    return []


def test_all_referenced_actions_are_full_sha_pinned() -> None:
    for path, document in _documents():
        for use in _uses_values(document):
            if use.startswith("./") or use.startswith("docker://"):
                continue
            reference = use.rsplit("@", 1)[-1]
            assert FULL_SHA.fullmatch(reference), f"mutable Action reference in {path}: {use}"


def test_public_workflows_have_read_only_permissions_and_no_privileged_trigger() -> None:
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        assert document["permissions"] == {"contents": "read"}
        assert "pull_request_target" not in text
        assert "self-hosted" not in text
        assert "secrets." not in text
        assert "packages: write" not in text


def test_composite_action_has_the_approved_thin_caller_contract() -> None:
    document = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    assert document["runs"]["using"] == "composite"
    assert set(document["inputs"]) >= {"package", "source", "tier", "target-python"}
    assert document["inputs"]["tier"]["default"] == "pull-request"
