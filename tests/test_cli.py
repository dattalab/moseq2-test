from typer.testing import CliRunner

from moseq2_test.cli import app

runner = CliRunner()


def test_version_reports_contract_versions() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "schema 1" in result.stdout
    assert "worker protocol 1" in result.stdout


def test_lists_all_public_profiles() -> None:
    result = runner.invoke(app, ["list", "profiles"])
    assert result.exit_code == 0
    for name in (
        "install-smoke",
        "historical-regression",
        "pipeline-smoke",
        "pipeline-end-to-end",
    ):
        assert name in result.output


def test_pipeline_end_to_end_is_explicitly_unavailable() -> None:
    result = runner.invoke(app, ["run", "pipeline-end-to-end"])
    assert result.exit_code == 4
    assert "profile_unavailable" in result.stderr


def test_help_exposes_complete_command_surface() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("doctor", "list", "data", "candidates", "run", "compare", "report", "baseline"):
        assert command in result.stdout
