# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector result models and the per-target identity-model enum.

All models are frozen (immutable after construction) via
:class:`pydantic.ConfigDict`. See v0.1-spec §"Versioned connectors + targets"
L267-292 (fingerprint shape) and §"Per-target identity model" L447-454
(AuthModel).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    extras: dict[str, Any] = Field(default_factory=dict)


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
    extras: dict[str, Any] = Field(default_factory=dict)
