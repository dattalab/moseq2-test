"""Contributor-facing command line interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from platformdirs import user_cache_path
from rich.console import Console
from rich.table import Table

from moseq2_test import SCHEMA_VERSION, WORKER_PROTOCOL_VERSION, __version__
from moseq2_test.doctor import inspect_host
from moseq2_test.errors import ExitCode, Moseq2TestError
from moseq2_test.registry import profiles
from moseq2_test.reporting import rerender

console = Console(stderr=True)
DEFAULT_CACHE_DIR = user_cache_path("moseq2-test")
app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
data_app = typer.Typer(no_args_is_help=True)
candidates_app = typer.Typer(no_args_is_help=True)
compare_app = typer.Typer(no_args_is_help=True)
baseline_app = typer.Typer(no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(candidates_app, name="candidates")
app.add_typer(compare_app, name="compare")
app.add_typer(baseline_app, name="baseline")


@dataclass
class Context:
    config: Path | None
    cache_dir: Path
    workspace: Path
    output_dir: Path
    executor: str
    target_python: Path | None
    container: str | None
    offline: bool
    verbosity: str


def _version(value: bool) -> None:
    if value:
        identity = (
            f"moseq2-test {__version__} "
            f"(schema {SCHEMA_VERSION}, worker protocol {WORKER_PROTOCOL_VERSION})"
        )
        typer.echo(identity)
        raise typer.Exit(ExitCode.ACCEPTED)


@app.callback()
def global_options(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)] = None,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE_DIR,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path(".moseq2-test/workspace"),
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("runs"),
    executor: Annotated[str, typer.Option("--executor")] = "process",
    target_python: Annotated[Path | None, typer.Option("--target-python")] = None,
    container: Annotated[str | None, typer.Option("--container")] = None,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    verbosity: Annotated[str, typer.Option("--verbosity")] = "normal",
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False,
) -> None:
    """Run installed-package and cross-pipeline MoSeq2 regression tests."""
    del no_color, version
    if executor not in {"process", "container"}:
        raise typer.BadParameter("executor must be process or container")
    if verbosity not in {"quiet", "normal", "verbose", "debug"}:
        raise typer.BadParameter("invalid verbosity")
    ctx.obj = Context(
        config=config,
        cache_dir=cache_dir,
        workspace=workspace,
        output_dir=output_dir,
        executor=executor,
        target_python=target_python,
        container=container,
        offline=offline,
        verbosity=verbosity,
    )


def _abort(error: Moseq2TestError) -> None:
    console.print(f"[red]{error.code}:[/red] {error}")
    raise typer.Exit(error.exit_code)


@app.command()
def doctor(
    ctx: typer.Context,
    ci: Annotated[bool, typer.Option("--ci", help="Apply hosted-CI resource checks")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check host tools, disk, executor, and target Python."""
    options: Context = ctx.obj
    result = inspect_host(options.workspace, target_python=options.target_python)
    required_free = 10 * 1024**3 if ci else 1 * 1024**3
    result["required_free_bytes"] = required_free
    result["accepted"] = result["disk"]["free"] >= required_free
    if json_output:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        typer.echo(f"controller: {result['controller']['version'].split()[0]}")
        typer.echo(f"workspace free: {result['disk']['free'] / 1024**3:.1f} GiB")
        typer.echo(f"preflight: {'accepted' if result['accepted'] else 'insufficient disk'}")
    if not result["accepted"]:
        raise typer.Exit(ExitCode.MISSING_INPUT)


@app.command("list")
def list_resources(
    what: Annotated[
        str, typer.Argument(help="profiles, packages, fixture-sets, or comparators")
    ] = "profiles",
) -> None:
    """List versioned built-in resources."""
    if what == "profiles":
        table = Table("Profile", "Status", "Description")
        for item in profiles():
            table.add_row(
                item.name, "implemented" if item.implemented else "unavailable", item.description
            )
        console.print(table)
        return
    if what == "packages":
        from moseq2_test.registry import source_lock

        for source_item in source_lock("moseq2-baseline-v1").sources:
            typer.echo(source_item.name)
        return
    if what == "fixture-sets":
        typer.echo("historical-v1\npipeline-smoke-v1")
        return
    if what == "comparators":
        from moseq2_test.compare.registry import comparator_names

        typer.echo("\n".join(comparator_names()))
        return
    raise typer.BadParameter(f"unknown resource class: {what}")


@data_app.command("fetch")
def data_fetch(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    fixture_set: Annotated[list[str] | None, typer.Option("--fixture-set")] = None,
    mirror: Annotated[Path | None, typer.Option("--mirror")] = None,
    extract: Annotated[
        bool,
        typer.Option("--extract/--no-extract", help="Prepare safe read-only archive expansions"),
    ] = False,
) -> None:
    """Fetch and verify immutable fixture objects."""
    from moseq2_test.data import fetch_selected

    options: Context = ctx.obj
    try:
        result = fetch_selected(
            options.cache_dir,
            profile_name=profile,
            fixture_sets=fixture_set or [],
            mirror=mirror,
            offline=options.offline,
            prepare_extracted=extract,
        )
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@data_app.command("verify")
def data_verify(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    fixture_set: Annotated[list[str] | None, typer.Option("--fixture-set")] = None,
) -> None:
    """Verify cached fixture objects without downloading them."""
    from moseq2_test.data import verify_selected

    options: Context = ctx.obj
    try:
        result = verify_selected(
            options.cache_dir, profile_name=profile, fixture_sets=fixture_set or []
        )
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@data_app.command("publish")
def data_publish(
    ctx: typer.Context,
    fixture_set: Annotated[str, typer.Option("--fixture-set")],
    source_root: Annotated[Path, typer.Option("--source-root", exists=True, file_okay=False)],
    dry_run: Annotated[bool, typer.Option("--dry-run/--execute")] = True,
) -> None:
    """Publish create-only fixture objects and verify anonymous reads."""
    from moseq2_test.data import publish_fixture_set

    options: Context = ctx.obj
    try:
        result = publish_fixture_set(
            fixture_set, source_root=source_root, cache_dir=options.cache_dir, dry_run=dry_run
        )
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@candidates_app.command("build")
def candidates_build(
    ctx: typer.Context,
    source: Annotated[list[str], typer.Option("--source")],
    allow_dirty_source: Annotated[bool, typer.Option("--allow-dirty-source")] = False,
) -> None:
    """Build source candidates into wheels in isolated sandboxes."""
    from moseq2_test.candidates import build_sources

    options: Context = ctx.obj
    try:
        result = build_sources(
            source,
            workspace=options.workspace,
            output=options.output_dir / "candidate-build",
            allow_dirty=allow_dirty_source,
            build_python=options.target_python,
        )
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


@candidates_app.command("verify")
def candidates_verify(
    candidate_set: Annotated[Path, typer.Option("--candidate-set", exists=True, dir_okay=False)],
) -> None:
    """Validate wheel identities and reject editable/source leaks."""
    from moseq2_test.candidates import load_candidate_set, verify_candidate_set

    try:
        result = load_candidate_set(candidate_set)
        verify_candidate_set(result, base=candidate_set.parent)
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command()
def run(
    ctx: typer.Context,
    profile_name: Annotated[str, typer.Argument(metavar="PROFILE")],
    package: Annotated[list[str] | None, typer.Option("--package")] = None,
    source: Annotated[list[str] | None, typer.Option("--source")] = None,
    candidate: Annotated[list[str] | None, typer.Option("--candidate")] = None,
    test_source: Annotated[list[str] | None, typer.Option("--test-source")] = None,
    candidate_set: Annotated[Path | None, typer.Option("--candidate-set")] = None,
    baseline_lock: Annotated[str, typer.Option("--baseline-lock")] = "moseq2-baseline-v1",
    fixture_set: Annotated[str | None, typer.Option("--fixture-set")] = None,
    intentional_change: Annotated[Path | None, typer.Option("--intentional-change")] = None,
    start_at: Annotated[str | None, typer.Option("--start-at")] = None,
    through: Annotated[str | None, typer.Option("--through")] = None,
    step: Annotated[list[str] | None, typer.Option("--step")] = None,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    keep_sandbox: Annotated[str, typer.Option("--keep-sandbox")] = "failure",
    timeout: Annotated[int | None, typer.Option("--timeout")] = None,
    jobs: Annotated[int, typer.Option("--jobs", min=1)] = 1,
    allow_dirty_source: Annotated[bool, typer.Option("--allow-dirty-source")] = False,
) -> None:
    """Run a named suite profile in an isolated target environment."""
    from moseq2_test.suites import RunOptions, run_profile

    options: Context = ctx.obj
    try:
        result = run_profile(
            RunOptions(
                profile=profile_name,
                packages=package or [],
                sources=source or [],
                candidates=candidate or [],
                test_sources=test_source or [],
                candidate_set=candidate_set,
                baseline_lock=baseline_lock,
                fixture_set=fixture_set,
                intentional_change=intentional_change,
                start_at=start_at,
                through=through,
                steps=step or [],
                seed=seed,
                keep_sandbox=keep_sandbox,
                timeout=timeout,
                jobs=jobs,
                allow_dirty_source=allow_dirty_source,
                cache_dir=options.cache_dir,
                workspace=options.workspace,
                output_dir=options.output_dir,
                executor=options.executor,
                target_python=options.target_python,
                container=options.container,
                offline=options.offline,
            )
        )
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(str(result.run_directory))
    raise typer.Exit(result.exit_code)


@compare_app.command("artifact")
def compare_artifact(
    kind: Annotated[str, typer.Option("--kind")],
    expected: Annotated[Path, typer.Option("--expected", exists=True, dir_okay=False)],
    actual: Annotated[Path, typer.Option("--actual", exists=True, dir_okay=False)],
    policy: Annotated[str, typer.Option("--policy")],
    intentional_change: Annotated[
        Path | None, typer.Option("--intentional-change", exists=True, dir_okay=False)
    ] = None,
) -> None:
    """Semantically compare two artifacts."""
    from moseq2_test.compare.registry import compare, load_intentional_change

    change = load_intentional_change(intentional_change) if intentional_change else None
    result = compare(kind, expected, actual, policy, intentional_change=change)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.status not in {"equal", "expected-change"}:
        raise typer.Exit(ExitCode.DIFFERENCE)


@compare_app.command("run")
def compare_run(
    expected_run: Annotated[Path, typer.Option("--expected-run", exists=True, file_okay=False)],
    actual_run: Annotated[Path, typer.Option("--actual-run", exists=True, file_okay=False)],
) -> None:
    """Compare canonical semantics of two recorded run directories."""
    from moseq2_test.compare.registry import compare_runs

    result = compare_runs(expected_run, actual_run)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.status not in {"equal", "expected-change"}:
        raise typer.Exit(ExitCode.DIFFERENCE)


@app.command()
def report(run_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)]) -> None:
    """Regenerate Markdown and JUnit views from canonical run JSON."""
    record = rerender(run_dir)
    typer.echo(f"rendered {record.run_id}")


@baseline_app.command("certify")
def baseline_certify(
    ctx: typer.Context,
    baseline_lock: Annotated[str, typer.Option("--baseline-lock")] = "moseq2-baseline-v1",
    fixture_set: Annotated[list[str] | None, typer.Option("--fixture-set")] = None,
) -> None:
    """Run the complete approved baseline parity matrix."""
    from moseq2_test.suites import certify_baseline

    options: Context = ctx.obj
    try:
        result = certify_baseline(
            baseline_lock=baseline_lock,
            fixture_sets=fixture_set or ["historical-v1", "pipeline-smoke-v1"],
            cache_dir=options.cache_dir,
            workspace=options.workspace,
            output_dir=options.output_dir,
            executor=options.executor,
            target_python=options.target_python,
            container=options.container,
            offline=options.offline,
        )
    except Moseq2TestError as error:
        _abort(error)
    typer.echo(str(result))


if __name__ == "__main__":
    app()
