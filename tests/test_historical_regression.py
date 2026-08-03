from __future__ import annotations

import hashlib
import io
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

from moseq2_test.failure_policy import junit_observations
from moseq2_test.models import SourceRecord
from moseq2_test.registry import profile
from moseq2_test.sandbox import Sandbox
from moseq2_test.suites.historical_regression import (
    _extract_locked_source_archive,
    _parse_junit,
    _prepare_test_tree,
    _test_command,
)


def test_profile_has_one_informative_step_per_locked_package() -> None:
    selected = profile("historical-regression")
    assert selected.implemented is True
    assert len(selected.steps) == 8
    assert [step.id for step in selected.steps] == [
        f"historical-tests-{package}" for package in selected.packages
    ]


def test_junit_summary_preserves_failures_and_collection_errors(tmp_path: Path) -> None:
    suite = ET.Element("testsuite", tests="3", failures="1", errors="1", skipped="0", time="0.25")
    path = tmp_path / "junit.xml"
    path.write_bytes(ET.tostring(suite, encoding="utf-8", xml_declaration=True))
    assert _parse_junit(path) == {
        "tests": 3,
        "failures": 1,
        "errors": 1,
        "skipped": 0,
        "time": 0.25,
        "parsed": True,
    }


def test_collection_exception_is_normalized_from_pytest_junit(tmp_path: Path) -> None:
    suite = ET.Element("testsuite", tests="1", failures="0", errors="1")
    case = ET.SubElement(suite, "testcase", classname="", name="collection failure")
    error = ET.SubElement(case, "error", message="collection failure")
    error.text = "E   ImportError: cannot import name 'old_api'"
    path = tmp_path / "junit.xml"
    path.write_bytes(ET.tostring(suite, encoding="utf-8", xml_declaration=True))
    observations = junit_observations(
        path,
        scope="historical-regression",
        package="moseq2-app",
        collection_nodeid="tests/gui_tests/test_main.py",
    )
    assert observations[0].outcome == "error"
    assert observations[0].exception == "ImportError"
    assert observations[0].nodeid == "tests/gui_tests/test_main.py"


def test_test_snapshot_cannot_import_the_source_package(tmp_path: Path) -> None:
    source = tmp_path / "snapshot"
    (source / "pybasicbayes").mkdir(parents=True)
    (source / "pybasicbayes" / "__init__.py").write_text("raise AssertionError\n")
    (source / "tests").mkdir()
    (source / "tests" / "test_public.py").write_text("def test_public(): pass\n")
    sandbox = Sandbox.create(tmp_path / "workspace")
    record = SourceRecord(
        name="pybasicbayes",
        repository="https://github.com/mattjj/pybasicbayes.git",
        commit="a" * 40,
        tree="b" * 40,
        default_branch="master",
        owner="mattjj",
        license_id="MIT",
    )
    destination, evidence = _prepare_test_tree(
        "pybasicbayes",
        record,
        source_override=source,
        source_mirror=None,
        source_archive=None,
        sandbox=sandbox,
        cache_dir=tmp_path / "cache",
        offline=True,
        allow_dirty=False,
    )
    assert not (destination / "pybasicbayes").exists()
    assert (destination / "tests" / "test_public.py").is_file()
    assert evidence["import_root_removed"] is True


def test_locked_source_archive_is_hash_verified_and_safely_staged(tmp_path: Path) -> None:
    archive_path = tmp_path / "pybasicbayes.tar.gz"
    payload = b"def test_public(): pass\n"
    with tarfile.open(archive_path, "w:gz") as archive:
        directory = tarfile.TarInfo("pybasicbayes")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        tests = tarfile.TarInfo("pybasicbayes/tests")
        tests.type = tarfile.DIRTYPE
        archive.addfile(tests)
        member = tarfile.TarInfo("pybasicbayes/tests/test_public.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    record = SourceRecord(
        name="pybasicbayes",
        repository="https://github.com/mattjj/pybasicbayes.git",
        commit="a" * 40,
        tree="b" * 40,
        default_branch="master",
        owner="mattjj",
        license_id="MIT",
    )
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    destination = tmp_path / "sources" / "pybasicbayes"
    destination.parent.mkdir()

    evidence = _extract_locked_source_archive(record, archive_path, digest, destination)

    assert (destination / "tests" / "test_public.py").read_bytes() == payload
    assert evidence["commit"] == record.commit
    assert evidence["source_archive_sha256"] == digest


def test_profile_command_becomes_an_explicit_target_command(tmp_path: Path) -> None:
    step = profile("historical-regression").steps[0]
    command = _test_command(step, tmp_path / "bin" / "python", tmp_path / "junit.xml")
    assert command[:3] == [str(tmp_path / "bin" / "python"), "-m", "pytest"]
    assert command[-1] == f"--junitxml={tmp_path / 'junit.xml'}"
