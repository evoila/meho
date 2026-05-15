# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request/response models for the audit-query REST surface (G8.1-T2).

The single non-trivial model is :class:`AuditQueryRequest` — the POST
body for ``/api/v1/audit/query``. It mirrors
:class:`~meho_backplane.audit_query.AuditQueryFilters` from the T1
substrate but accepts ``since`` / ``until`` as strings so operators can
pass shorthand (``"24h"`` / ``"7d"``) instead of ISO-8601. The router
converts them via :func:`~meho_backplane.audit_query.parse_duration`
before dispatch.

The response models (:class:`AuditEntry` / :class:`AuditQueryResult`)
are re-exported from the substrate without modification so OpenAPI
surfaces them under the audit-query tag without duplicating the schema.

Tenant scoping by construction
==============================

:class:`AuditQueryRequest` deliberately has **no** ``tenant_id`` field.
Pydantic v2's default ``extra="ignore"`` policy means a client that
puts ``tenant_id`` in the body has the value silently dropped; the
router never reads from the body for the tenant boundary — it injects
``operator.tenant_id`` from the JWT into the T1 handler call. The
substrate's mandatory keyword-only ``tenant_id`` argument enforces the
invariant on its side.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.audit_query import AuditEntry, AuditQueryResult

__all__ = [
    "AuditEntry",
    "AuditQueryRequest",
    "AuditQueryResult",
]


class AuditQueryRequest(BaseModel):
    """POST body for ``/api/v1/audit/query``.

    Mirrors :class:`~meho_backplane.audit_query.AuditQueryFilters`
    except ``since`` / ``until`` are strings parsed by
    :func:`~meho_backplane.audit_query.parse_duration` at the router
    layer. All other fields pass through to the substrate filter
    object unchanged. The empty body shape ``{}`` is the no-filter
    case — every field defaults to None or the substrate-side default.
    """

    model_config = ConfigDict(frozen=True)

    target: str | None = Field(default=None, max_length=256)
    principal: str | None = Field(default=None, max_length=256)
    op_id: str | None = Field(default=None, max_length=256)
    op_class: str | None = Field(default=None, max_length=64)
    result_status: str | None = Field(default=None, max_length=16)
    since: str | None = Field(default=None, max_length=32)
    until: str | None = Field(default=None, max_length=32)
    audit_id: uuid.UUID | None = None
    parent_audit_id: uuid.UUID | None = None
    agent_session_id: uuid.UUID | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = Field(default=None, max_length=512)
