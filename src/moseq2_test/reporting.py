"""Canonical run records and deterministic human/test projections."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from moseq2_test.models import RunRecord


def canonical_json(record: RunRecord) -> str:
    return json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def render_markdown(record: RunRecord) -> str:
    lines = [
        f"# moseq2-test run `{record.run_id}`",
        "",
        f"- Profile: `{record.profile}`",
        f"- Status: `{record.status}`",
        f"- Framework: `{record.framework_version}`",
        f"- Worker protocol: `{record.worker_protocol_version}`",
        f"- Seed: `{record.seed}`",
        f"- Commands: `{len(record.commands)}`",
        f"- Comparisons: `{len(record.comparisons)}`",
    ]
    if record.failure_stage:
        lines.append(f"- Failure stage: `{record.failure_stage}`")
    lines.extend(["", "## Commands", ""])
    lines.append("| ID | Classification | Return code | Timed out |")
    lines.append("|---|---|---:|---|")
    for command in record.commands:
        lines.append(
            f"| `{command.id}` | `{command.classification}` | "
            f"{command.returncode if command.returncode is not None else ''} | "
            f"{str(command.timed_out).lower()} |"
        )
    lines.extend(["", "## Comparisons", ""])
    if record.comparisons:
        for result in record.comparisons:
            lines.append(f"- `{result.kind}`: **{result.status}** — {result.summary}")
    else:
        lines.append("No comparisons were recorded.")
    return "\n".join(lines) + "\n"


def render_junit(record: RunRecord) -> bytes:
    failures = sum(
        command.classification not in {"passed", "known-failure", "allowed-pass"}
        for command in record.commands
    )
    suite = ET.Element(
        "testsuite",
        name=record.profile,
        tests=str(len(record.commands)),
        failures=str(failures),
        errors="0",
    )
    for command in record.commands:
        case = ET.SubElement(suite, "testcase", name=command.id, classname=record.profile)
        if command.classification not in {"passed", "known-failure", "allowed-pass"}:
            failure = ET.SubElement(case, "failure", message=command.classification)
            failure.text = command.stderr or command.stdout or "command failed"
    payload: bytes = ET.tostring(suite, encoding="utf-8", xml_declaration=True)
    return payload


def write_run_directory(
    root: Path,
    record: RunRecord,
    *,
    resolved_config: dict[str, object] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "comparisons").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "manifests").mkdir(exist_ok=True)
    (root / "outputs").mkdir(exist_ok=True)
    (root / "run.json").write_text(canonical_json(record), encoding="utf-8")
    (root / "summary.md").write_text(render_markdown(record), encoding="utf-8")
    (root / "junit.xml").write_bytes(render_junit(record))
    (root / "resolved-config.yml").write_text(
        yaml.safe_dump(resolved_config or {}, sort_keys=True), encoding="utf-8"
    )
    (root / "candidates.json").write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in record.candidates], indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "environment.json").write_text(
        json.dumps(record.environment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for index, comparison in enumerate(record.comparisons):
        safe_kind = re.sub(r"[^A-Za-z0-9_.-]", "_", comparison.kind)
        (root / "comparisons" / f"{index:03d}-{safe_kind}.json").write_text(
            json.dumps(comparison.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def load_run(root: Path) -> RunRecord:
    return RunRecord.model_validate_json((root / "run.json").read_text(encoding="utf-8"))


def rerender(root: Path) -> RunRecord:
    record = load_run(root)
    (root / "summary.md").write_text(render_markdown(record), encoding="utf-8")
    (root / "junit.xml").write_bytes(render_junit(record))
    return record
