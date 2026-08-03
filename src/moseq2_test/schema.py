"""Generate and verify the checked-in JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from moseq2_test.models import MODEL_SCHEMAS


def generate_schemas(output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in sorted(MODEL_SCHEMAS.items()):
        path = output / f"{name}.schema.json"
        payload = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        path.write_text(payload, encoding="utf-8")
        written.append(path)
    return written


def verify_schemas(root: Path) -> list[str]:
    differences: list[str] = []
    for name, model in sorted(MODEL_SCHEMAS.items()):
        path = root / f"{name}.schema.json"
        expected = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        if not path.is_file():
            differences.append(f"missing {path.name}")
        elif path.read_text(encoding="utf-8") != expected:
            differences.append(f"stale {path.name}")
    return differences
