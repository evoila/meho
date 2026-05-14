# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the targets surface (G0.3 T2).

Four models cover the full CRUD + list contract:

* :class:`Target` — full read shape. Returned by GET /targets/{name}
  and by the resolver. Frozen.
* :class:`TargetSummary` — short shape for list responses. Only the
  fields needed to identify and filter; omits ``notes`` and ``extras``
  to keep list payloads small. Frozen.
* :class:`TargetCreate` — POST body. All required fields explicit; optional
  fields have documented defaults matching the ORM column defaults.
* :class:`TargetUpdate` — PATCH body. Every field optional; only fields
  that are not ``None`` are applied by the route handler. ``name`` and
  ``product`` are intentionally absent — rename = delete + create.

``AuthModel`` is imported from :mod:`meho_backplane.connectors.schemas`
(G0.2-T1) and re-used here so the enum value set stays in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.connectors.schemas import AuthModel

__all__ = [
    "AuthModel",
    "Target",
    "TargetCreate",
    "TargetSummary",
    "TargetUpdate",
]


class TargetSummary(BaseModel):
    """Short shape for list endpoints.

    Omits ``notes``, ``extras``, and connection-auth details to keep
    list responses fast and small. The ``aliases`` field is included
    because list consumers (CLI ``meho target list``, autocomplete)
    need it to display secondary names.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    aliases: tuple[str, ...]
    product: str
    host: str


class Target(BaseModel):
    """Full read shape — returned by GET /targets/{name} and the resolver.

    Maps 1:1 to the ``targets`` table columns. Frozen so callers
    can safely stash instances in request state or structured logs
    without fear of mutation.

    ``aliases`` uses ``tuple[str, ...]`` and ``extras`` uses
    ``Mapping[str, Any]`` so frozen instances cannot be mutated in-place
    via list.append / dict.__setitem__ — matching the immutability contract
    the docstring documents.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str
    aliases: tuple[str, ...]
    product: str
    host: str
    port: int | None
    fqdn: str | None
    secret_ref: str | None
    auth_model: AuthModel
    vpn_required: bool
    extras: Mapping[str, Any]
    notes: str | None
    created_at: datetime
    updated_at: datetime


class TargetCreate(BaseModel):
    """POST /api/v1/targets body.

    ``name`` and ``product`` are immutable after creation; to rename a
    target, delete + re-create. ``auth_model`` defaults to
    ``shared_service_account`` matching the DB column default.
    """

    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    product: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    fqdn: str | None = None
    secret_ref: str | None = None
    auth_model: AuthModel = AuthModel.SHARED_SERVICE_ACCOUNT
    vpn_required: bool = False
    extras: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class TargetUpdate(BaseModel):
    """PATCH /api/v1/targets/{name} body.

    All fields are optional. The route handler applies only the fields
    that are not ``None``; callers must send an explicit ``null`` JSON
    value to clear a nullable column (``fqdn``, ``secret_ref``,
    ``notes``). ``name`` and ``product`` are absent — rename = delete
    + create.
    """

    aliases: list[str] | None = None
    host: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    fqdn: str | None = None
    secret_ref: str | None = None
    auth_model: AuthModel | None = None
    vpn_required: bool | None = None
    extras: dict[str, Any] | None = None
    notes: str | None = None
