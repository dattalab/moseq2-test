"""Typed failures and stable CLI exit codes."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    ACCEPTED = 0
    DIFFERENCE = 1
    INVALID = 2
    MISSING_INPUT = 3
    UNAVAILABLE = 4
    INFRASTRUCTURE = 5


class Moseq2TestError(RuntimeError):
    exit_code = ExitCode.INFRASTRUCTURE
    code = "infrastructure_error"


class InvalidConfiguration(Moseq2TestError):
    exit_code = ExitCode.INVALID
    code = "invalid_configuration"


class MissingInput(Moseq2TestError):
    exit_code = ExitCode.MISSING_INPUT
    code = "missing_input"


class ProfileUnavailable(Moseq2TestError):
    exit_code = ExitCode.UNAVAILABLE
    code = "profile_unavailable"


class UnexpectedResult(Moseq2TestError):
    exit_code = ExitCode.DIFFERENCE
    code = "unexpected_result"
