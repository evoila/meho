# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``AgentDefinitionService`` -- tenant-scoped CRUD over ``agent_definition``.

G11.1-T2 (#809) under Initiative #802. The single code path the REST
routes (:mod:`meho_backplane.api.v1.agents`), MCP verbs
(:mod:`meho_backplane.mcp.tools.agents`), and Go CLI verbs
(``cli/internal/cmd/agent``) all dispatch through, so the tenant
boundary and the per-tenant name natural-key contract are enforced in
one place.

Concurrency model
-----------------

The service is **stateless and method-scoped**: each public method
opens its own :class:`~sqlalchemy.ext.asyncio.AsyncSession` via
:func:`~meho_backplane.db.engine.get_sessionmaker`, commits, and
closes -- no shared transaction state across calls. This mirrors the
:class:`~meho_backplane.memory.service.MemoryService` /
:class:`~meho_backplane.kb.service.KbService` precedents: instantiate
once per request (or once per long-running CLI session) and call
freely.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter -- no
contextvar resolution. The caller (route / MCP handler / CLI) binds it
from the operator's JWT. Every query starts with
``WHERE tenant_id = :tenant_id`` so cross-tenant rows are structurally
invisible: a ``get`` / ``update`` / ``delete`` against another
tenant's definition returns ``None`` / ``False`` (the 404 the route
renders), never the other tenant's row.

RBAC
----

This service does **not** enforce roles -- it assumes the caller has
already validated the tenant role (``tenant_admin`` for writes,
``operator`` for reads). Splitting RBAC out keeps the service callable
from contexts where the role discipline differs (an unattended
provisioning job, a future seeding migration). The REST routes / MCP
tools / CLI verbs own the :func:`~meho_backplane.auth.rbac.require_role`
gate.

Error contract
--------------

* :class:`AgentDefinitionExistsError` -- a create collided with an
  existing ``(tenant_id, name)`` row (the
  ``agent_definition_tenant_name_idx`` unique-index violation). The
  REST route maps it to 409; the MCP tool to an invalid-params error.
* Read / update / delete signal *absence* via ``None`` / ``False``
  rather than an exception, so the 404-vs-existence-leak collapse stays
  trivial at the boundary.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from meho_backplane.agents.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionRead,
    AgentDefinitionUpdate,
    validate_name,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition

__all__ = ["AgentDefinitionExistsError", "AgentDefinitionService"]


#: Default per-call paging cap for :meth:`AgentDefinitionService.list_`.
#: Agent corpora per tenant are small (a handful of named agents), so
#: the default returns every row in one shot; the cap stays in place so
#: a tenant that grows past it doesn't accidentally stream the whole
#: table back to a casual ``meho agent list``.
DEFAULT_LIST_LIMIT: int = 100


class AgentDefinitionExistsError(Exception):
    """Raised when a create collides with an existing ``(tenant_id, name)``.

    Carries the conflicting *name* so the boundary layer can render a
    precise message. The REST route maps this to 409 Conflict; the MCP
    tool maps it to an invalid-params error.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"agent definition {name!r} already exists for this tenant")


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether *exc* is a unique-constraint violation.

    Both dialects covered, mirroring
    :func:`~meho_backplane.api.v1.broadcast_overrides.create_override_impl`'s
    detection: PG (asyncpg) exposes the SQLSTATE code via
    ``orig.sqlstate`` -- ``23505`` is ``unique_violation``; SQLite emits
    the documented ``UNIQUE constraint failed`` substring. The
    ``pgcode`` fallback survives in case a future psycopg-based wiring
    shows up. A non-unique :class:`IntegrityError` (e.g. an FK
    violation for a typo'd tenant id) returns ``False`` so it propagates
    as a 500 rather than being misreported as a duplicate.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    orig_msg = str(orig or exc)
    return sqlstate == "23505" or "UNIQUE constraint failed" in orig_msg


class AgentDefinitionService:
    """Tenant-scoped CRUD over :class:`~meho_backplane.db.models.AgentDefinition`.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session, commits, and closes -- no shared
    transaction state across calls. The class ships with no constructor
    parameters: the session-per-method shape rules out a caller-owned
    session, and the engine is bound via the module-level singleton the
    chassis already set up.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def create(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: AgentDefinitionCreate,
    ) -> AgentDefinitionRead:
        """Create one agent definition under *tenant_id*.

        Validates the name at the service boundary (in addition to the
        :class:`AgentDefinitionCreate` field pattern) so a direct,
        non-Pydantic caller cannot smuggle a path-breaking name past
        the gate.

        Raises
        ------
        AgentDefinitionExistsError
            When ``(tenant_id, name)`` already exists -- the
            ``agent_definition_tenant_name_idx`` unique-index violation,
            narrowed from a generic :class:`IntegrityError`.
        ValueError
            When *name* contains characters outside the safe set.
        """
        validate_name(payload.name)
        row = AgentDefinition(
            tenant_id=tenant_id,
            name=payload.name,
            identity_ref=payload.identity_ref,
            model_tier=payload.model_tier.value,
            system_prompt=payload.system_prompt,
            toolset=payload.toolset,
            turn_budget=payload.turn_budget,
            output_schema=payload.output_schema,
            enabled=payload.enabled,
            created_by_sub=created_by_sub,
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                if _is_unique_violation(exc):
                    raise AgentDefinitionExistsError(payload.name) from exc
                raise
            # Refresh so DB-side defaults (created_at / updated_at on PG)
            # are visible on the returned row before the session closes.
            await session.refresh(row)
            entry = AgentDefinitionRead.model_validate(row)
            await session.commit()
        self._log.info(
            "agent_definition_create",
            tenant_id=str(tenant_id),
            name=payload.name,
            created_by_sub=created_by_sub,
        )
        return entry

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[AgentDefinitionRead]:
        """Return up to *limit* definitions for *tenant_id*, name-sorted.

        Pure list -- the ``meho agent list`` / ``GET /api/v1/agents``
        backend. Sorted by name so output is predictable across runs.
        Tenant scoping is the first WHERE clause: a tenant only ever
        sees its own definitions.

        ``limit`` / ``offset`` raise :class:`ValueError` on negative
        values so a misconfigured caller surfaces at the boundary rather
        than silently truncating.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentDefinition)
                .where(AgentDefinition.tenant_id == tenant_id)
                .order_by(AgentDefinition.name)
                .limit(limit)
                .offset(offset)
            )
            rows = result.scalars().all()
        return [AgentDefinitionRead.model_validate(row) for row in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> AgentDefinitionRead | None:
        """Fetch one definition by ``(tenant_id, name)``; ``None`` if absent.

        Backs ``meho agent show`` / ``GET /api/v1/agents/{name}``. Name
        is not re-validated -- an out-of-shape name simply matches no
        row and yields ``None``; the boundary renders that as 404. The
        ``None`` return is also what a cross-tenant probe receives (the
        ``tenant_id`` WHERE clause excludes other tenants' rows), so
        existence is never leaked across the tenant boundary.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentDefinition).where(
                    AgentDefinition.tenant_id == tenant_id,
                    AgentDefinition.name == name,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return AgentDefinitionRead.model_validate(row)

    async def update(
        self,
        tenant_id: uuid.UUID,
        name: str,
        payload: AgentDefinitionUpdate,
    ) -> AgentDefinitionRead | None:
        """Apply a partial update to ``(tenant_id, name)``; ``None`` if absent.

        Only fields the caller explicitly set are applied
        (``model_dump(exclude_unset=True)``), so a PATCH can change one
        field without clobbering the rest. ``name`` is not updatable (it
        is the natural key) -- the field is absent from
        :class:`AgentDefinitionUpdate`. ``model_tier`` is stored as its
        string value.

        Returns ``None`` when no row matches (absent or cross-tenant) so
        the boundary renders 404. The ``onupdate`` ORM hook bumps
        ``updated_at`` on any column change.
        """
        changes = payload.model_dump(exclude_unset=True)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AgentDefinition).where(
                    AgentDefinition.tenant_id == tenant_id,
                    AgentDefinition.name == name,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for field, value in changes.items():
                # model_tier round-trips through the enum's .value so the
                # column stores the wire string, not "AgentModelTier.X".
                if field == "model_tier" and value is not None:
                    value = value.value if hasattr(value, "value") else value
                setattr(row, field, value)
            await session.flush()
            await session.refresh(row)
            entry = AgentDefinitionRead.model_validate(row)
            await session.commit()
        self._log.info(
            "agent_definition_update",
            tenant_id=str(tenant_id),
            name=name,
            fields=sorted(changes.keys()),
        )
        return entry

    async def delete(
        self,
        tenant_id: uuid.UUID,
        name: str,
    ) -> bool:
        """Delete the definition matching ``(tenant_id, name)``.

        Returns ``True`` when a row was deleted, ``False`` when none
        matched (absent or cross-tenant). The boundary translates
        ``False`` to 404 -- never 403, so the existence of a definition
        is not leaked across the tenant boundary.

        Uses ``DELETE ... RETURNING name`` to detect the no-row case
        without relying on the dialect-specific ``CursorResult.rowcount``
        (the async :class:`~sqlalchemy.engine.Result` typing surface
        mypy sees does not expose ``rowcount``; SQLite + PG both support
        ``DELETE ... RETURNING``).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                delete(AgentDefinition)
                .where(
                    AgentDefinition.tenant_id == tenant_id,
                    AgentDefinition.name == name,
                )
                .returning(AgentDefinition.name)
            )
            deleted = result.scalar_one_or_none() is not None
            await session.commit()
        self._log.info(
            "agent_definition_delete",
            tenant_id=str(tenant_id),
            name=name,
            deleted=deleted,
        )
        return deleted
