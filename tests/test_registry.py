import pytest

from moseq2_test.errors import ProfileUnavailable
from moseq2_test.registry import profile


def test_unavailable_profile_fails_closed() -> None:
    with pytest.raises(ProfileUnavailable):
        profile("pipeline-end-to-end")


def test_unavailable_profile_can_be_inspected() -> None:
    value = profile("pipeline-end-to-end", require_implemented=False)
    assert value.implemented is False
    assert value.name == "pipeline-end-to-end"
