# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``AgentGrantService`` — tenant-scoped CRUD over ``agent_permission``.

G11.2-T6 (#819) under Initiative #803 (the P3 agent identity + RBAC +
approval gate). The single code path the REST routes
(:mod:`meho_backplane.api.v1.agent_grants`), MCP verbs
(:mod:`meho_backplane.mcp.tools.agent_grants`), and Go CLI verbs
(``cli/internal/cmd/agent/grant.go``) all dispatch through, so the
tenant boundary and the ``expires_at`` validation contract are enforced
in one place.

What this service does
----------------------

* **Grant** — insert one ``agent_permission`` row for a principal with a
  verdict and an optional expiry (time-bounded elevation).
* **Revoke** — delete one row by id; cross-tenant revoke returns
  ``False`` (404 at the boundary) without revealing whether the row
  exists in another tenant.
* **List** — page through grants for a tenant (optionally filtered by
  ``principal_sub``). Active-only by default (``expires_at IS NULL OR
  expires_at > now()``).

Default-deny contract
---------------------

A new agent (principal) has **no** ``agent_permission`` rows by default.
The permission resolver (G11.2-T3, ``auth/permissions.py``) falls back
to ``safety_level``-based defaults when no rows match:

* ``safe`` → ``auto-execute``
* ``caution`` → ``needs-approval``
* ``dangerous`` → ``deny``

Until a ``tenant_admin`` issues a grant, the agent is effectively
read-only — admin-class (dangerous) ops are never auto-granted.

Elevation pattern
-----------------

A grant with ``expires_at`` set is a time-bounded elevation (change
window). The grant-expiry sweeper
(:mod:`meho_backplane.agents.grant_expiry`) periodically deletes rows
whose ``expires_at < now()``, reverting the agent to its baseline
permissions without any operator action.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter — no
contextvar resolution. Every query starts with
``WHERE tenant_id = :tenant_id``.

RBAC
----

This service does **not** enforce roles — callers own the
``require_role(TenantRole.TENANT_ADMIN)`` gate. The design mirrors
:class:`~meho_backplane.agents.service.AgentDefinitionService`.

Error contract
--------------

* :exc:`GrantValidationError` — ``expires_at`` is in the past, or
  ``target_scope`` is neither ``None``, ``"*"``, nor a valid UUID
  string. The boundary maps this to 422.
* ``None`` / ``False`` signals absence on get / revoke so the boundary
  renders 404 without revealing cross-tenant existence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from fnmatch import fnmatch

import structlog
from sqlalchemy import delete, select

from meho_backplane.agents.grant_schemas import AgentGrantCreate, AgentGrantRead
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission

__all__ = ["AgentGrantService", "GrantValidationError"]

_log = structlog.get_logger(__name__)

#: Default paging cap for :meth:`AgentGrantService.list_`.
DEFAULT_LIST_LIMIT: int = 100


class GrantValidationError(Exception):
    """Raised for semantic validation failures on grant creation.

    Covers two cases:
    * ``expires_at`` is in the past.
    * ``target_scope`` is neither ``None``, ``"*"``, nor a valid UUID.

    The REST route maps this to HTTP 422; the MCP tool maps it to
    ``McpInvalidParamsError``.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _validate_target_scope(target_scope: str | None) -> None:
    """Raise :exc:`GrantValidationError` when *target_scope* is invalid.

    Valid values: ``None`` (= any target), ``"*"`` (explicit any-target),
    or a UUID-parseable string (exactly one target).
    """
    if target_scope is None or target_scope == "*":
        return
    try:
        uuid.UUID(target_scope)
    except ValueError:
        raise GrantValidationError(
            f"target_scope {target_scope!r} is not a valid UUID string or '*'; "
            "use '*' for any target or a UUID for a specific one"
        ) from None


def _validate_op_pattern(op_pattern: str) -> None:
    """Raise :exc:`GrantValidationError` when *op_pattern* is structurally invalid.

    The only structural check is that the pattern is non-empty and that
    ``fnmatch.fnmatch`` can evaluate it without raising. This is a
    fail-fast guard; operational correctness (does this pattern match
    what you think it matches?) is the caller's responsibility.
    """
    if not op_pattern:
        raise GrantValidationError("op_pattern must be a non-empty string")
    # Confirm fnmatch can evaluate the pattern without exploding.
    try:
        fnmatch("probe.op.id", op_pattern)
    except Exception as exc:
        raise GrantValidationError(
            f"op_pattern {op_pattern!r} is not a valid fnmatch glob: {exc}"
        ) from exc


def _validate_expires_at(expires_at: datetime | None) -> None:
    """Raise :exc:`GrantValidationError` when *expires_at* is in the past."""
    if expires_at is None:
        return
    # Normalise to UTC for the comparison; naive datetimes are rejected.
    if expires_at.tzinfo is None:
        raise GrantValidationError("expires_at must be a timezone-aware datetime (UTC preferred)")
    if expires_at <= datetime.now(UTC):
        raise GrantValidationError(
            f"expires_at {expires_at.isoformat()} is in the past; "
            "a time-bounded elevation must expire in the future"
        )


class AgentGrantService:
    """Tenant-scoped grant management over :class:`~meho_backplane.db.models.AgentPermission`.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session, commits, and closes — no shared
    transaction state. The class has no constructor parameters (mirrors
    :class:`~meho_backplane.agents.service.AgentDefinitionService`).
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def grant(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: AgentGrantCreate,
    ) -> AgentGrantRead:
        """Create one permission grant row.

        Validates ``expires_at``, ``target_scope``, and ``op_pattern``
        at the service boundary before inserting.

        Parameters
        ----------
        tenant_id:
            Tenant the grant belongs to (from the operator's JWT).
        created_by_sub:
            JWT ``sub`` of the issuing ``tenant_admin``.
        payload:
            Validated :class:`~meho_backplane.agents.grant_schemas.AgentGrantCreate`.

        Returns
        -------
        AgentGrantRead
            The freshly inserted row, refreshed so DB-side defaults
            (``created_at`` on PG) are visible.

        Raises
        ------
        GrantValidationError
            When semantic validation fails (past expiry, bad UUID scope).
        """
        _validate_op_pattern(payload.op_pattern)
        _validate_target_scope(payload.target_scope)
        _validate_expires_at(payload.expires_at)

        row = AgentPermission(
            tenant_id=tenant_id,
            principal_sub=payload.principal_sub,
            op_pattern=payload.op_pattern,
            target_scope=payload.target_scope,
            verdict=payload.verdict.value,
            created_by_sub=created_by_sub,
            expires_at=payload.expires_at,
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(row)
            await session.flush()
            await session.refresh(row)
            entry = AgentGrantRead.model_validate(row)
            await session.commit()

        self._log.info(
            "agent_grant_created",
            tenant_id=str(tenant_id),
            principal_sub=payload.principal_sub,
            op_pattern=payload.op_pattern,
            verdict=payload.verdict.value,
            expires_at=payload.expires_at.isoformat() if payload.expires_at else None,
            grant_id=str(entry.id),
            created_by_sub=created_by_sub,
        )
        return entry

    async def revoke(
        self,
        tenant_id: uuid.UUID,
        grant_id: uuid.UUID,
    ) -> bool:
        """Delete the grant matching ``(tenant_id, grant_id)``.

        Returns ``True`` when a row was deleted, ``False`` when none
        matched (absent or cross-tenant). The boundary translates
        ``False`` to 404.

        Cross-tenant revoke attempts silently return ``False`` — the
        ``tenant_id`` WHERE clause excludes other tenants' rows so
        existence is not leaked.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                delete(AgentPermission)
                .where(
                    AgentPermission.tenant_id == tenant_id,
                    AgentPermission.id == grant_id,
                )
                .returning(AgentPermission.id)
            )
            deleted = result.scalar_one_or_none() is not None
            await session.commit()

        self._log.info(
            "agent_grant_revoked",
            tenant_id=str(tenant_id),
            grant_id=str(grant_id),
            deleted=deleted,
        )
        return deleted

    async def get(
        self,
        tenant_id: uuid.UUID,
        grant_id: uuid.UUID,
    ) -> AgentGrantRead | None:
        """Fetch one grant by ``(tenant_id, grant_id)``; ``None`` if absent."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentPermission).where(
                    AgentPermission.tenant_id == tenant_id,
                    AgentPermission.id == grant_id,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return AgentGrantRead.model_validate(row)

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        principal_sub: str | None = None,
        include_expired: bool = False,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[AgentGrantRead]:
        """Return up to *limit* grants for *tenant_id*, newest-first.

        Parameters
        ----------
        tenant_id:
            Tenant to list grants for.
        principal_sub:
            Optional filter — only return grants for this principal.
        include_expired:
            When ``False`` (default), rows with ``expires_at < now()``
            are excluded. When ``True``, all rows (including past
            elevations) are returned.
        limit:
            Page size cap (default 100).
        offset:
            Page offset (default 0).
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []

        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(AgentPermission)
                .where(AgentPermission.tenant_id == tenant_id)
                .order_by(AgentPermission.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if principal_sub is not None:
                stmt = stmt.where(AgentPermission.principal_sub == principal_sub)
            if not include_expired:
                # Include permanent grants (expires_at IS NULL) and
                # still-active elevations (expires_at > now).
                stmt = stmt.where(
                    (AgentPermission.expires_at.is_(None)) | (AgentPermission.expires_at > now)
                )
            result = await session.execute(stmt)
            rows = result.scalars().all()
        return [AgentGrantRead.model_validate(row) for row in rows]
