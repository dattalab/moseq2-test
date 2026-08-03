"""Validated controller side of the standalone worker protocol."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkerRequest(ProtocolModel):
    protocol_version: Literal[1] = 1
    request_id: str
    operation: str
    parameters: dict[str, Any]


class WorkerResponse(ProtocolModel):
    protocol_version: Literal[1]
    request_id: str
    status: Literal["ok", "error"]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
