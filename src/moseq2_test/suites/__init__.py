"""Profile selection and orchestration entry points."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from moseq2_test.errors import ProfileUnavailable
from moseq2_test.registry import profile


@dataclass(frozen=True)
class RunOptions:
    profile: str
    packages: list[str]
    sources: list[str]
    candidates: list[str]
    test_sources: list[str]
    candidate_set: Path | None
    baseline_lock: str
    fixture_set: str | None
    intentional_change: Path | None
    start_at: str | None
    through: str | None
    steps: list[str]
    seed: int
    keep_sandbox: str
    timeout: int | None
    jobs: int
    allow_dirty_source: bool
    cache_dir: Path
    workspace: Path
    output_dir: Path
    executor: str
    target_python: Path | None
    container: str | None
    offline: bool


@dataclass(frozen=True)
class SuiteRun:
    run_directory: Path
    exit_code: int


def run_profile(options: RunOptions) -> SuiteRun:
    selected = profile(options.profile, require_implemented=False)
    if not selected.implemented:
        raise ProfileUnavailable(f"profile {selected.name!r} is defined but unavailable")
    if selected.name == "install-smoke":
        from moseq2_test.suites.install_smoke import run_install_smoke

        run_directory, exit_code = run_install_smoke(options, selected)
        return SuiteRun(run_directory=run_directory, exit_code=exit_code)
    if selected.name == "historical-regression":
        from moseq2_test.suites.historical_regression import run_historical_regression

        run_directory, exit_code = run_historical_regression(options, selected)
        return SuiteRun(run_directory=run_directory, exit_code=exit_code)
    if selected.name == "pipeline-smoke":
        from moseq2_test.suites.pipeline_smoke import run_pipeline_smoke

        run_directory, exit_code = run_pipeline_smoke(options, selected)
        return SuiteRun(run_directory=run_directory, exit_code=exit_code)
    raise ProfileUnavailable(f"profile {selected.name!r} has no installed runner yet")


def certify_baseline(**_options: object) -> Path:
    raise ProfileUnavailable(
        "baseline certification is defined but unavailable until profiles are migrated"
    )
