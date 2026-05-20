# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

r"""Tenant-admin CRUD surface for :class:`BroadcastOverride` rows.

G6.3-T4 (#381) under Initiative #376. T1 (#378) ships the schema; T2
(#379) ships the per-tenant resolver + cache; T3 (#380) ships the
per-call opt-in transport; T4 (this module) ships the operator-facing
management plane for the durable per-tenant rules.

Three verbs:

* ``GET /api/v1/broadcast/overrides`` -- list the operator's tenant's
  rules. Optional ``op_id_pattern`` query parameter filters by exact
  pattern match (for "does my k8s.configmap.info rule exist?" UX).
* ``POST /api/v1/broadcast/overrides`` -- create a rule. Returns 201
  with the new row. ``IntegrityError`` on the composite-unique index
  → 409.
* ``DELETE /api/v1/broadcast/overrides/{id}`` -- delete a rule owned
  by the operator's tenant. Returns 204. Cross-tenant probes return
  404 (never 403 -- existence is not leaked across tenant boundaries).

RBAC
====

Every route is gated by ``Depends(require_role(TenantRole.TENANT_ADMIN))``
-- the most restrictive built-in tier. Operators and read-only tokens
hit 403 ``insufficient_role`` from
:func:`~meho_backplane.auth.rbac.require_role` before the handler
runs. Mirrors the same dependency surface every chassis route uses.

Tenant-scoping invariant
========================

Every database query starts with ``WHERE tenant_id = operator.tenant_id``.
The operator's tenant is lifted from the JWT by
:func:`~meho_backplane.auth.rbac.require_role`; client-supplied tenant
values are never honored. Cross-tenant rows are invisible: a GET
returns only the operator's tenant's rows; a DELETE on another
tenant's id returns 404 (the row genuinely "doesn't exist" from this
operator's view).

Self-observability
==================

Every CRUD route binds ``audit_op_id`` / ``audit_op_class`` /
``audit_override_*`` contextvars before the handler returns, so the
chassis :class:`~meho_backplane.audit.AuditMiddleware` produces:

* An audit row with the override diff in ``payload``
  (``override_op``, ``override_id``, ``override_pattern``,
  ``override_detail``).
* A broadcast event under ``op_class=write`` (POST/DELETE) or
  ``op_class=read`` (GET) so colleagues see "operator X created /
  removed override Y" in the SSE feed.

Cache invalidation
==================

Every successful mutation calls
:func:`~meho_backplane.broadcast.overrides.invalidate_tenant_cache`
so the next publish for the tenant reloads from the DB instead of
serving up-to-60s-stale cached rules. Listing reads from DB directly
(no cache involvement).

Glob-not-regex validation
=========================

``op_id_pattern`` accepts globs (``*`` plus literals) per Initiative
#376's "no regex" decision. The router rejects any pattern containing
characters suggesting regex syntax (``[``, ``(``, ``\``, ``+``, ``?``
not at the trailing position). False positives on a literal
``[bracket]`` are acceptable -- operators don't need brackets for the
op-id vocabulary the resolver actually walks.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.broadcast.overrides import invalidate_tenant_cache
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import BroadcastOverride

__all__ = ["router"]


router = APIRouter(prefix="/api/v1/broadcast/overrides", tags=["broadcast"])


#: Canonical op_id every list call emits via the ``audit_op_id``
#: contextvar override honoured by
#: :func:`~meho_backplane.audit._publish_broadcast_event`. Set/remove
#: use distinct op_ids so ``meho audit query`` can filter to "rule
#: mutations" vs reads.
_OP_ID_LIST: Final[str] = "meho.broadcast.overrides.list"
_OP_ID_SET: Final[str] = "meho.broadcast.overrides.set"
_OP_ID_REMOVE: Final[str] = "meho.broadcast.overrides.remove"


#: Characters that suggest regex syntax. A pattern containing any of
#: these is rejected at the API layer -- glob-only per Initiative
#: #376. ``*`` is allowed because that's the glob wildcard; ``?`` is
#: rejected outright because globs technically support it but the
#: Initiative's op-id vocabulary doesn't need single-char wildcards
#: and rejecting it removes one footgun.
_REGEX_LIKE_CHARS: Final[frozenset[str]] = frozenset("[](){}\\+?|^$")


#: Module-level :class:`Depends` closure -- built once at import time
#: to satisfy ruff's B008 rule, mirrors the convention in
#: :mod:`~meho_backplane.api.v1.audit` and
#: :mod:`~meho_backplane.api.v1.retrieve_usage`.
_require_tenant_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class BroadcastOverrideCreate(BaseModel):
    """Incoming POST body. Pydantic v2 strict.

    ``extra="forbid"`` rejects unknown fields with 422 -- catches a
    client typo (``"scope-field": "namespace"`` with the wrong kebab
    case) before it silently lands as a no-op.

    ``model_validator(mode="after")`` enforces the scope-pair
    consistency invariant: ``scope_field`` and ``scope_value`` must
    both be NULL (op-wide rule) or both be set (scoped rule). A half-
    set pair is a client bug.

    ``op_id_pattern`` is validated against the regex-character
    blacklist by a second model_validator.
    """

    model_config = ConfigDict(extra="forbid")

    op_id_pattern: str = Field(min_length=1, max_length=128)
    scope_field: Literal["namespace", "target_name"] | None = None
    scope_value: str | None = Field(default=None, max_length=128)
    detail: Literal["full", "aggregate"]

    @model_validator(mode="after")
    def _scope_pair_must_be_consistent(self) -> BroadcastOverrideCreate:
        if (self.scope_field is None) ^ (self.scope_value is None):
            raise ValueError(
                "scope_field and scope_value must both be set or both be NULL",
            )
        return self

    @model_validator(mode="after")
    def _op_id_pattern_must_not_be_regex(self) -> BroadcastOverrideCreate:
        bad = sorted({c for c in self.op_id_pattern if c in _REGEX_LIKE_CHARS})
        if bad:
            raise ValueError(
                f"op_id_pattern must be glob, not regex; rejected chars: {bad}",
            )
        return self


class BroadcastOverrideRead(BaseModel):
    """Outgoing row representation.

    ``model_config = ConfigDict(from_attributes=True)`` lets the
    handler return the SQLAlchemy ORM object directly; FastAPI
    serialises via this model rather than the ORM's ``__dict__``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    op_id_pattern: str
    scope_field: str | None
    scope_value: str | None
    detail: str
    created_by_sub: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Audit / broadcast contextvar plumbing
# ---------------------------------------------------------------------------


def _bind_list_audit() -> None:
    """Audit row for the list verb: read class, no diff."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_LIST,
        audit_op_class="read",
    )


def _bind_set_audit(*, override_id: uuid.UUID, pattern: str, detail: str) -> None:
    """Audit row for the set verb: write class, diff in payload.

    ``audit_override_*`` contextvars surface through the chassis
    :func:`~meho_backplane.audit._resolve_audit_payload` (which strips
    the ``audit_`` prefix), landing on the audit row's payload as
    ``override_op="set"``, ``override_id=<uuid>``,
    ``override_pattern=<str>``, ``override_detail="full"|"aggregate"``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_SET,
        audit_op_class="write",
        audit_override_op="set",
        audit_override_id=str(override_id),
        audit_override_pattern=pattern,
        audit_override_detail=detail,
    )


def _bind_remove_audit(*, override_id: uuid.UUID) -> None:
    """Audit row for the remove verb: write class, ``override_op="remove"``."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_ID_REMOVE,
        audit_op_class="write",
        audit_override_op="remove",
        audit_override_id=str(override_id),
    )


# ---------------------------------------------------------------------------
# Implementation functions
# ---------------------------------------------------------------------------
#
# Extracted from the route handlers so the G6.3-T5 admin MCP tools
# (``meho.broadcast.overrides.list|set|remove``) can call into the same
# code path in-process. The route handlers below are thin wrappers
# around these; the MCP tools call them with a transient session +
# concrete :class:`Operator`. Same RBAC (every call site is gated by
# ``require_role(TENANT_ADMIN)``), same audit + cache-invalidation
# hooks, same :class:`HTTPException` shapes (the MCP tool wraps those
# into ``McpInvalidParamsError`` for the JSON-RPC envelope).


async def list_overrides_impl(
    *,
    operator: Operator,
    session: AsyncSession,
    op_id_pattern: str | None = None,
) -> list[BroadcastOverride]:
    """List override rules owned by the operator's tenant.

    Tenant-scoping is the first WHERE clause -- a probe with a known
    other-tenant pattern always returns an empty list, never the
    other tenant's rules. ``op_id_pattern`` is an exact-match filter
    (the rule's stored pattern equals the query parameter); to find
    rules whose pattern would *match* a given op_id, dump the full
    list and apply :func:`fnmatch.fnmatchcase` client-side.
    """
    _bind_list_audit()
    stmt = select(BroadcastOverride).where(
        BroadcastOverride.tenant_id == operator.tenant_id,
    )
    if op_id_pattern is not None:
        stmt = stmt.where(BroadcastOverride.op_id_pattern == op_id_pattern)
    stmt = stmt.order_by(BroadcastOverride.created_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_override_impl(
    *,
    payload: BroadcastOverrideCreate,
    operator: Operator,
    session: AsyncSession,
) -> BroadcastOverride:
    """Create an override rule; broadcasts as ``op_class=write``.

    ``IntegrityError`` on the composite-unique index (same
    ``(tenant_id, op_id_pattern, scope_field, scope_value)``) maps
    to 409 with a clear detail. Other DB errors propagate as 500
    (the chassis turns them into the standard error response).
    """
    row = BroadcastOverride(
        tenant_id=operator.tenant_id,
        op_id_pattern=payload.op_id_pattern,
        scope_field=payload.scope_field,
        scope_value=payload.scope_value,
        detail=payload.detail,
        created_by_sub=operator.sub,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Narrow the 409 to actual composite-unique-index violations.
        # Other IntegrityError shapes (FK violation -- impossible in
        # practice thanks to ``ensure_tenant``, but defensively
        # handled here; future NOT NULL or CHECK constraint adds)
        # propagate so a genuine corruption surfaces as a 500 rather
        # than a misleading "already exists" message.
        #
        # PG via asyncpg: ``PostgresError`` exposes the SQLSTATE code
        # through ``orig.sqlstate`` -- asyncpg's exceptions/_base.py
        # field map binds character ``'C'`` to ``sqlstate`` (not
        # ``pgcode``). The earlier ``pgcode`` check was inherited from
        # the psycopg2 shape and silently returned ``None`` against
        # asyncpg; SQLite tests passed only via the substring fallback
        # below. The ``pgcode`` fallback survives in case a future
        # psycopg-based wiring shows up. SQLite: the
        # ``UNIQUE constraint failed`` substring is the documented
        # form (sqlite.org/lang_conflict.html). Both dialects covered.
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        orig_msg = str(orig or exc)
        is_unique_violation = sqlstate == "23505" or "UNIQUE constraint failed" in orig_msg
        if is_unique_violation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="broadcast_override_already_exists",
            ) from exc
        raise
    # Refresh so the DB-side defaults (created_at, updated_at) are
    # visible on the returned row; the SQLAlchemy session would
    # otherwise hand back stale Python-default values.
    await session.refresh(row)
    _bind_set_audit(
        override_id=row.id,
        pattern=row.op_id_pattern,
        detail=row.detail,
    )
    invalidate_tenant_cache(operator.tenant_id)
    return row


async def delete_override_impl(
    *,
    override_id: uuid.UUID,
    operator: Operator,
    session: AsyncSession,
) -> None:
    """Delete an override rule owned by the operator's tenant.

    Cross-tenant 404: the DELETE statement filters by both ``id`` AND
    ``tenant_id``. A row that exists but belongs to another tenant
    matches zero rows, and the handler raises 404 -- never 403,
    because a 403/404 split would leak the existence of an override
    id across the tenant boundary.
    """
    # ``RETURNING id`` lets us detect the no-row-deleted case without
    # relying on the dialect-specific ``CursorResult.rowcount`` (the
    # async ``Result`` typing surface mypy sees here does not expose
    # ``rowcount``; SQLite + PG both support ``DELETE ... RETURNING``).
    stmt = (
        delete(BroadcastOverride)
        .where(
            BroadcastOverride.id == override_id,
            BroadcastOverride.tenant_id == operator.tenant_id,
        )
        .returning(BroadcastOverride.id)
    )
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="broadcast_override_not_found",
        )
    _bind_remove_audit(override_id=override_id)
    invalidate_tenant_cache(operator.tenant_id)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BroadcastOverrideRead])
async def list_overrides(
    operator: Annotated[Operator, _require_tenant_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
    op_id_pattern: Annotated[
        str | None,
        Query(
            max_length=128,
            description="Exact-match filter on op_id_pattern (not a glob match).",
        ),
    ] = None,
) -> list[BroadcastOverride]:
    """List override rules owned by the operator's tenant."""
    return await list_overrides_impl(
        operator=operator,
        session=session,
        op_id_pattern=op_id_pattern,
    )


@router.post(
    "",
    response_model=BroadcastOverrideRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_override(
    payload: BroadcastOverrideCreate,
    operator: Annotated[Operator, _require_tenant_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BroadcastOverride:
    """Create an override rule; broadcasts as ``op_class=write``."""
    return await create_override_impl(
        payload=payload,
        operator=operator,
        session=session,
    )


@router.delete("/{override_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_override(
    override_id: uuid.UUID,
    operator: Annotated[Operator, _require_tenant_admin],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete an override rule owned by the operator's tenant."""
    await delete_override_impl(
        override_id=override_id,
        operator=operator,
        session=session,
    )
