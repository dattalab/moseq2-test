"""Exact known-failure and unexpected-pass classification."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from moseq2_test.models import FailurePolicy, KnownFailure, KnownFailuresManifest


@dataclass(frozen=True)
class ObservedFailure:
    scope: str
    package: str | None
    outcome: str
    message: str
    exception: str | None = None
    nodeid: str | None = None
    step_id: str | None = None


@dataclass(frozen=True)
class FailureClassification:
    target: str
    status: str
    known_failure_id: str | None
    message: str


def _target(value: ObservedFailure | KnownFailure) -> str:
    return value.nodeid or value.step_id or "<unknown>"


def _applicable(
    known: KnownFailure,
    *,
    scope: str,
    package: str | None,
    baseline_commits: dict[str, str],
) -> bool:
    if known.scope != scope or (known.package is not None and known.package != package):
        return False
    return all(
        baseline_commits.get(name) == commit for name, commit in known.baseline_commits.items()
    )


def _matches(observed: ObservedFailure, known: KnownFailure) -> bool:
    if _target(observed) != _target(known) or observed.outcome != known.outcome:
        return False
    if known.signature.exception and observed.exception != known.signature.exception:
        return False
    return re.search(known.signature.message_regex, observed.message, re.DOTALL) is not None


def classify_failures(
    observations: list[ObservedFailure],
    manifest: KnownFailuresManifest,
    *,
    scope: str,
    package: str | None,
    baseline_commits: dict[str, str],
) -> list[FailureClassification]:
    applicable = [
        item
        for item in manifest.failures
        if _applicable(
            item,
            scope=scope,
            package=package,
            baseline_commits=baseline_commits,
        )
    ]
    matched: set[str] = set()
    results: list[FailureClassification] = []
    for observation in observations:
        match = next((item for item in applicable if _matches(observation, item)), None)
        if match is None:
            results.append(
                FailureClassification(
                    target=_target(observation),
                    status="unexpected-failure",
                    known_failure_id=None,
                    message=observation.message,
                )
            )
        else:
            matched.add(match.id)
            results.append(
                FailureClassification(
                    target=_target(observation),
                    status="known-failure",
                    known_failure_id=match.id,
                    message=observation.message,
                )
            )
    for known in applicable:
        if known.id in matched:
            continue
        status = "allowed-pass" if known.policy == FailurePolicy.ALLOWED else "unexpected-pass"
        results.append(
            FailureClassification(
                target=_target(known),
                status=status,
                known_failure_id=known.id,
                message="the expected failure was not observed",
            )
        )
    return results


def junit_observations(
    path: Path,
    *,
    scope: str,
    package: str,
    collection_nodeid: str | None = None,
) -> list[ObservedFailure]:
    root = ET.parse(path).getroot()
    results: list[ObservedFailure] = []
    for case in root.iter("testcase"):
        child = next((item for item in case if item.tag in {"failure", "error"}), None)
        if child is None:
            continue
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        if collection_nodeid and child.tag == "error" and "collection" in name.lower():
            nodeid = collection_nodeid
        else:
            parts = classname.split(".")
            test_index = next(
                (index for index, value in enumerate(parts) if value.startswith("test_")), None
            )
            if test_index is None:
                nodeid = collection_nodeid or f"{classname}::{name}"
            else:
                file_part = "/".join(parts[: test_index + 1]) + ".py"
                class_part = parts[test_index + 1 :]
                nodeid = "::".join([file_part, *class_part, name])
        message = "\n".join(value for value in (child.attrib.get("message"), child.text) if value)
        exception = child.attrib.get("type")
        if exception is None:
            match = re.search(r"(?:^|\n)([A-Za-z][A-Za-z0-9_.]*(?:Error|Exception)):", message)
            exception = match.group(1).split(".")[-1] if match else None
        results.append(
            ObservedFailure(
                scope=scope,
                package=package,
                nodeid=nodeid,
                outcome="error" if child.tag == "error" else "failed",
                exception=exception,
                message=message,
            )
        )
    return results


def classifications_accepted(values: list[FailureClassification]) -> bool:
    return all(value.status in {"known-failure", "allowed-pass"} for value in values)
