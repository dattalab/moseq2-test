import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from moseq2_test.config import load_yaml
from moseq2_test.models import MODEL_SCHEMAS, SuiteProfile
from moseq2_test.registry import (
    fixture_manifest,
    known_failures,
    profiles,
    source_lock,
    wheel_lock,
)
from moseq2_test.schema import generate_schemas, verify_schemas


def test_all_built_in_manifests_validate() -> None:
    assert len(profiles()) == 4
    assert len(source_lock("moseq2-baseline-v1").sources) == 8
    assert len(wheel_lock("moseq2-baseline-linux-py37-v1").wheels) == 8
    assert len(fixture_manifest("historical-v1").objects) == 5
    assert len(fixture_manifest("pipeline-smoke-v1").objects) == 20
    assert len(known_failures().failures) == 12


def test_unknown_schema_major_fails_closed() -> None:
    value = load_yaml(Path("profiles/pipeline-end-to-end.yml"))
    value["schema_version"] = 2
    with pytest.raises(ValidationError):
        SuiteProfile.model_validate(value)


def test_checked_in_schemas_match_models(tmp_path: Path) -> None:
    generated = generate_schemas(tmp_path)
    assert len(generated) == len(MODEL_SCHEMAS)
    for path in generated:
        assert json.loads(path.read_text())["type"] == "object"
    assert verify_schemas(tmp_path) == []
