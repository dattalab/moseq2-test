from __future__ import annotations

from pathlib import Path

from moseq2_test.registry import profile
from moseq2_test.suites.install_smoke import (
    _cli_result,
    _inspection_results,
    _selected_ids,
)
from moseq2_test.workers.legacy_worker import operation_compiled_smoke


def _inspection(prefix: Path) -> dict[str, object]:
    return {
        "python_version_info": [3, 7, 17],
        "python_version": "3.7.17",
        "python_executable": str(prefix / "bin" / "python"),
        "python_prefix": str(prefix),
        "editable_links": [],
        "pth_entries": [],
        "imports": [],
        "distributions": [],
        "extensions": [],
    }


def test_profile_preserves_the_exact_47_historical_check_names() -> None:
    selected = profile("install-smoke")
    identifiers = _selected_ids(selected, explicit=[], start=None, through=None)
    assert len(identifiers) == 47
    assert identifiers[0] == "python-version"
    assert identifiers[-1] == "compiled-operation-autoregressive"


def test_missing_extension_is_a_named_failure(tmp_path: Path) -> None:
    check_id = "extension-pyhsmm.util.cstats"
    inspection = _inspection(tmp_path / "target")
    inspection["extensions"] = [
        {"module": "pyhsmm.util.cstats", "imported": False, "error": "missing"}
    ]
    results = _inspection_results(
        {check_id},
        inspection,
        expected_prefix=tmp_path / "target",
        allowed_roots=[tmp_path / "target"],
        pip_check={"returncode": 0},
    )
    assert results[check_id].id == check_id
    assert results[check_id].classification == "failed"


def test_source_checkout_import_is_a_named_failure(tmp_path: Path) -> None:
    check_id = "import-moseq2_extract"
    inspection = _inspection(tmp_path / "target")
    inspection["imports"] = [
        {
            "module": "moseq2_extract",
            "imported": True,
            "file": str(tmp_path / "checkout" / "moseq2_extract" / "__init__.py"),
        }
    ]
    result = _inspection_results(
        {check_id},
        inspection,
        expected_prefix=tmp_path / "target",
        allowed_roots=[tmp_path / "target"],
        pip_check={"returncode": 0},
    )[check_id]
    assert result.classification == "failed"


def test_editable_and_escaped_pth_are_separate_named_failures(tmp_path: Path) -> None:
    inspection = _inspection(tmp_path / "target")
    inspection["editable_links"] = [str(tmp_path / "target" / "x.egg-link")]
    inspection["pth_entries"] = [
        {"file": str(tmp_path / "target" / "x.pth"), "entry": str(tmp_path / "checkout")}
    ]
    results = _inspection_results(
        {"no-editable-links", "no-escaped-pth"},
        inspection,
        expected_prefix=tmp_path / "target",
        allowed_roots=[tmp_path / "target"],
        pip_check={"returncode": 0},
    )
    assert results["no-editable-links"].classification == "failed"
    assert results["no-escaped-pth"].classification == "failed"


def test_missing_cli_is_a_named_failure(tmp_path: Path) -> None:
    check_id = "cli-moseq2-extract-help"
    result = _cli_result(
        check_id,
        path=tmp_path / "bin" / "moseq2-extract",
        argument="--help",
        script_record={"name": "moseq2-extract", "exists": False},
    )
    assert result.id == check_id
    assert result.classification == "failed"


def test_compiled_worker_reports_each_failure_without_collapsing() -> None:
    result = operation_compiled_smoke({})
    identifiers = [check["id"] for check in result["checks"]]
    assert identifiers == [
        "compiled-operation-pybasicbayes",
        "compiled-operation-pyhsmm-cstats",
        "compiled-operation-pyhsmm-hmm",
        "compiled-operation-autoregressive",
    ]
