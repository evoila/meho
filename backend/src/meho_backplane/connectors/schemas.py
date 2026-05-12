# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector result models and the per-target identity-model enum.

All models use ``ConfigDict(frozen=True)`` — field reassignment is blocked at
runtime. The ``extras`` field is wrapped in :class:`types.MappingProxyType` on
construction, so in-place mutation also raises :exc:`TypeError`. See v0.1-spec
§"Versioned connectors + targets" L267-292 (fingerprint shape) and §"Per-target
identity model" L447-454 (AuthModel).
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

__all__ = [
    "AuthModel",
    "FingerprintResult",
    "OperationResult",
    "ProbeResult",
]


class AuthModel(StrEnum):
    """Per-target identity model per v0.1-spec L447-454."""

    IMPERSONATION = "impersonation"
    SHARED_SERVICE_ACCOUNT = "shared_service_account"
    PER_USER = "per_user"


class FingerprintResult(BaseModel):
    """Connector fingerprint per consumer-needs L95."""

    model_config = ConfigDict(frozen=True)

    vendor: str
    product: str
    version: str | None = None
    build: str | None = None
    edition: str | None = None
    reachable: bool
    probed_at: datetime
    probe_method: str
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_extras(self) -> "FingerprintResult":
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))
        return self

    @field_serializer("extras")
    def _serialize_extras(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class ProbeResult(BaseModel):
    """Lightweight reachability + auth-challenge result."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str | None = None
    latency_ms: float | None = None
    probed_at: datetime


class OperationResult(BaseModel):
    """Connector op execution result."""

    model_config = ConfigDict(frozen=True)

    status: str  # "ok" | "error" | "denied"
    op_id: str
    result: dict[str, Any] | list[Any] | None = None
    error: str | None = None
    duration_ms: float
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_extras(self) -> "OperationResult":
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))
        return self

    @field_serializer("extras")
    def _serialize_extras(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)
