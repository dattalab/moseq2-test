from moseq2_test.failure_policy import (
    ObservedFailure,
    classifications_accepted,
    classify_failures,
)
from moseq2_test.registry import known_failures

EXTRACT_COMMIT = {"moseq2-extract": "39a6f0f88d28fc311c8be96619ee9e53b14d3a96"}


def observation(nodeid: str, message: str, exception: str = "ValueError") -> ObservedFailure:
    return ObservedFailure(
        scope="historical-regression",
        package="moseq2-extract",
        nodeid=nodeid,
        outcome="failed",
        exception=exception,
        message=message,
    )


def test_exact_known_failure_is_accepted_but_missing_deterministic_failures_are_not() -> None:
    values = classify_failures(
        [
            observation(
                "tests/integration_tests/test_cli.py::CLITests::test_extract",
                "ValueError: depth video has 20 frames but depth_ts.txt has 53801 timestamps",
            )
        ],
        known_failures(),
        scope="historical-regression",
        package="moseq2-extract",
        baseline_commits=EXTRACT_COMMIT,
    )
    assert values[0].status == "known-failure"
    assert any(value.status == "unexpected-pass" for value in values)
    assert classifications_accepted(values) is False


def test_unknown_failure_cannot_hide_behind_expected_count() -> None:
    values = classify_failures(
        [observation("tests/test_unknown.py::test_new", "AssertionError: new")],
        known_failures(),
        scope="historical-regression",
        package="moseq2-extract",
        baseline_commits=EXTRACT_COMMIT,
    )
    assert values[0].status == "unexpected-failure"


def test_allowed_nfs_failure_may_pass() -> None:
    manifest = known_failures()
    manifest = manifest.model_copy(
        update={
            "failures": [
                item
                for item in manifest.failures
                if item.id == "historical-extract-nfs-cleanup-race"
            ]
        }
    )
    values = classify_failures(
        [],
        manifest,
        scope="historical-regression",
        package="moseq2-extract",
        baseline_commits=EXTRACT_COMMIT,
    )
    assert [value.status for value in values] == ["allowed-pass"]
    assert classifications_accepted(values)


def test_signature_substitution_is_unexpected() -> None:
    values = classify_failures(
        [
            observation(
                "tests/integration_tests/test_cli.py::CLITests::test_extract",
                "ValueError: a different defect",
            )
        ],
        known_failures(),
        scope="historical-regression",
        package="moseq2-extract",
        baseline_commits=EXTRACT_COMMIT,
    )
    assert values[0].status == "unexpected-failure"
