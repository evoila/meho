# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector result models and the per-target identity-model enum.

All models use ``ConfigDict(frozen=True)`` — field reassignment is blocked at
runtime. The ``extras`` field is wrapped in :class:`types.MappingProxyType` on
construction, so in-place mutation also raises :exc:`TypeError`. See v0.1-spec
§"Versioned connectors + targets" L267-292 (fingerprint shape) and §"Per-target
identity model" L447-454 (AuthModel) and §"JSONFlux / result handles"
L294-311 (ResultHandle).

``ResultHandle`` lives here rather than in :mod:`meho_backplane.operations.reducer`
(despite the JSONFlux contract being a reducer concern) so that
:class:`OperationResult` can carry it as a first-class field without an
``operations → connectors`` cycle: ``operations.reducer`` already needs
``OperationResult`` indirectly (its ``Reducer`` Protocol surfaces values the
dispatcher wraps into ``OperationResult``), and the dispatcher's
``connectors → operations`` direction is forbidden in this codebase.
:mod:`meho_backplane.operations.reducer` re-exports ``ResultHandle`` so the
documented public path (``from meho_backplane.operations import ResultHandle``)
keeps working.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

__all__ = [
    "AuthModel",
    "FingerprintResult",
    "OperationResult",
    "ProbeResult",
    "ResultHandle",
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


class ResultHandle(BaseModel):
    """Reference to a set-shaped payload stored out-of-band (MinIO / S3 / …).

    JSONFlux contract per v0.1-spec L294-311. The v0.2 default reducer
    (:class:`~meho_backplane.operations.reducer.PassThroughReducer`) **never**
    produces a handle — the field on :class:`OperationResult` stays ``None``
    on every successful dispatch in v0.2. Real reduction lands in a separate
    Initiative once the first generic-ingested connectors produce real
    response payloads to calibrate against; that work populates the handle
    on >50-row / >4 KB set-shaped responses, stores the full payload in an
    addressable backing store (MinIO/S3/Valkey), and ships the
    ``result_query`` / ``result_aggregate`` meta-tools that read it back.

    ``schema_`` (trailing underscore) is the JSON Schema of the underlying
    payload — the trailing underscore avoids collision with Pydantic's own
    deprecated ``BaseModel.schema()`` API and is serialised as ``schema_``
    in the wire format. ``model_config = ConfigDict(frozen=True)`` so
    callers can't mutate a handle after the reducer hands it back.
    """

    model_config = ConfigDict(frozen=True)

    handle_id: UUID
    summary_md: str
    schema_: dict[str, Any] = Field(
        description="JSON Schema (Draft 2020-12) of the underlying payload shape.",
    )
    total_rows: int | None = None
    sample_rows: list[dict[str, Any]] | None = None
    ttl_seconds: int


class OperationResult(BaseModel):
    """Connector op execution result.

    ``handle`` is populated by JSONFlux-aware reducers when the raw response
    is set-shaped and large enough to spill out-of-band; v0.2's pass-through
    reducer always leaves it ``None``. Consumers should treat
    ``result + handle`` as a tagged union: ``result`` carries either the raw
    payload (no reduction) or the reduced summary (reduction happened), and
    ``handle is not None`` is the signal that the **full** payload is
    addressable via the reducer's backing store rather than inlined here.
    """

    model_config = ConfigDict(frozen=True)

    status: str  # "ok" | "error" | "denied"
    op_id: str
    result: dict[str, Any] | list[Any] | None = None
    error: str | None = None
    duration_ms: float
    handle: ResultHandle | None = None
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_extras(self) -> "OperationResult":
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))
        return self

    @field_serializer("extras")
    def _serialize_extras(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)
