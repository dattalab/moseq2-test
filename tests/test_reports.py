from datetime import UTC, datetime
from pathlib import Path

from moseq2_test.models import CommandResult, RunRecord
from moseq2_test.reporting import load_run, rerender, write_run_directory


def example_record() -> RunRecord:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    return RunRecord(
        run_id="20260803T000000Z-abcd1234",
        framework_version="0.1.0",
        worker_protocol_version=1,
        profile="install-smoke",
        status="accepted",
        started_at=now,
        completed_at=now,
        seed=0,
        commands=[
            CommandResult(
                id="import-moseq2_extract",
                command=["python", "-c", "import moseq2_extract"],
                returncode=0,
                duration_seconds=0.1,
                classification="passed",
            )
        ],
    )


def test_report_round_trip_is_deterministic(tmp_path: Path) -> None:
    record = example_record()
    write_run_directory(tmp_path, record)
    markdown = (tmp_path / "summary.md").read_bytes()
    junit = (tmp_path / "junit.xml").read_bytes()
    rerender(tmp_path)
    assert (tmp_path / "summary.md").read_bytes() == markdown
    assert (tmp_path / "junit.xml").read_bytes() == junit
    assert load_run(tmp_path) == record


def test_partial_failed_record_is_writable(tmp_path: Path) -> None:
    record = example_record().model_copy(update={"status": "failed", "failure_stage": "setup"})
    write_run_directory(tmp_path, record)
    assert load_run(tmp_path).failure_stage == "setup"
