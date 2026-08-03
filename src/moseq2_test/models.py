"""Versioned runtime contracts for manifests and run evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionedModel(StrictModel):
    schema_version: Literal[1] = 1


class TrustClass(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED_BASELINE = "trusted-baseline"
    GENERATED = "generated"


class PublicationStatus(StrEnum):
    APPROVED = "approved-by-project-owner"
    LOCAL_ONLY = "local-only"
    REFERENCE_ONLY = "reference-only"


class SourceRecord(StrictModel):
    name: str
    repository: HttpUrl
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    default_branch: str
    owner: str
    license_id: str


class SourceLock(VersionedModel):
    lock_id: str
    sources: list[SourceRecord]
    impact_graph: dict[str, list[str]] = Field(default_factory=dict)


class WheelRecord(StrictModel):
    package: str
    filename: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_tag: str
    abi_tag: str
    platform_tag: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WheelLock(VersionedModel):
    lock_id: str
    python: str
    platform: str
    wheels: list[WheelRecord]


class FixtureObject(StrictModel):
    id: str
    kind: str
    filename: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_urls: list[HttpUrl]
    provenance_urls: list[str] = Field(default_factory=list)
    publication_status: PublicationStatus
    trust: TrustClass
    unpack: Literal["none", "zip", "tar", "tar.gz"] = "none"
    archive_root: str | None = None
    profiles: list[str]
    license_id: str
    citation: str
    source: str
    max_unpacked_bytes: int | None = Field(default=None, ge=0)
    max_members: int | None = Field(default=None, ge=0)


class Derivation(StrictModel):
    id: str
    inputs: list[str]
    recipe: str
    outputs: list[str]


class FixtureManifest(VersionedModel):
    fixture_set: str
    status: Literal["immutable"]
    created_at: datetime
    maintainer: str
    terms: str
    objects: list[FixtureObject]
    derivations: list[Derivation] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_objects(self) -> FixtureManifest:
        identifiers = [item.id for item in self.objects]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("fixture object IDs must be unique")
        return self


class CandidateKind(StrEnum):
    SOURCE = "source"
    WHEEL = "wheel"


class CandidateRecord(StrictModel):
    package: str
    kind: CandidateKind
    location: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    test_source: str | None = None
    dirty: bool = False


class CandidateSet(VersionedModel):
    candidates: list[CandidateRecord]

    @model_validator(mode="after")
    def unique_packages(self) -> CandidateSet:
        names = [item.package for item in self.candidates]
        if len(names) != len(set(names)):
            raise ValueError("candidate package names must be unique")
        return self


class ResourceCeilings(StrictModel):
    cpu: int = Field(ge=1)
    memory_gib: float = Field(gt=0)
    disk_gib: float = Field(gt=0)
    timeout_seconds: int = Field(gt=0)


class SuiteStep(StrictModel):
    id: str
    package: str | None = None
    stage: str
    operation: str
    command: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    required: bool = True
    fixtures: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    comparator_policy: str | None = None
    expected_failure: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)


class SuiteProfile(VersionedModel):
    name: str
    implemented: bool
    owner: str
    description: str
    packages: list[str]
    fixture_sets: list[str]
    stages: list[str]
    resources: ResourceCeilings
    steps: list[SuiteStep]

    @model_validator(mode="after")
    def valid_dag(self) -> SuiteProfile:
        identifiers = [step.id for step in self.steps]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("suite step IDs must be unique")
        known: set[str] = set()
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"step {step.id} has forward/unknown dependencies: {missing}")
            known.add(step.id)
        return self


class FailurePolicy(StrEnum):
    REQUIRED = "required_failure"
    ALLOWED = "allowed_failure"


class FailureSignature(StrictModel):
    exception: str | None = None
    message_regex: str


class KnownFailure(StrictModel):
    id: str
    scope: str
    package: str | None = None
    nodeid: str | None = None
    step_id: str | None = None
    baseline_commits: dict[str, str]
    outcome: Literal["failed", "error"]
    signature: FailureSignature
    policy: FailurePolicy
    owner: str
    review_by: datetime
    evidence: str


class KnownFailuresManifest(VersionedModel):
    failures: list[KnownFailure]


class IntentionalChange(VersionedModel):
    id: str
    old_expectation: str
    new_expectation: str
    affected_artifacts: list[str]
    issue_or_pr: str
    rationale: str
    regression_test: str
    reviewer: str
    approved_at: datetime


class ComparisonStatus(StrEnum):
    EQUAL = "equal"
    EXPECTED_CHANGE = "expected-change"
    DIFFERENT = "different"
    INVALID = "invalid"
    ERROR = "error"


class ComparisonResult(VersionedModel):
    status: ComparisonStatus
    kind: str
    comparator: str
    comparator_version: str
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    policy: str
    tolerances: dict[str, float] = Field(default_factory=dict)
    ignored_fields: list[str] = Field(default_factory=list)
    differences: list[dict[str, Any]] = Field(default_factory=list)
    summary: str


class CommandResult(StrictModel):
    id: str
    command: list[str]
    returncode: int | None = None
    duration_seconds: float = Field(ge=0)
    stdout: str | None = None
    stderr: str | None = None
    timed_out: bool = False
    classification: str


class RunRecord(VersionedModel):
    run_id: str
    framework_version: str
    worker_protocol_version: int
    profile: str
    status: str
    failure_stage: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    seed: int
    source_lock: str | None = None
    wheel_lock: str | None = None
    fixture_sets: list[str] = Field(default_factory=list)
    candidates: list[CandidateRecord] = Field(default_factory=list)
    commands: list[CommandResult] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    known_failure_results: list[dict[str, Any]] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    retained_outputs: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


MODEL_SCHEMAS: dict[str, type[BaseModel]] = {
    "candidate-set": CandidateSet,
    "comparison-result": ComparisonResult,
    "fixture-manifest": FixtureManifest,
    "intentional-change": IntentionalChange,
    "known-failures": KnownFailuresManifest,
    "run-record": RunRecord,
    "source-lock": SourceLock,
    "suite-profile": SuiteProfile,
    "wheel-lock": WheelLock,
}
