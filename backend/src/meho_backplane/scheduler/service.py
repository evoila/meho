# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``SchedulerAdminService`` -- tenant-scoped CRUD over ``scheduled_trigger``.

G11.3-T5 (#826) under Initiative #804. The single code path the REST
routes (:mod:`meho_backplane.api.v1.scheduler`), MCP verbs
(:mod:`meho_backplane.mcp.tools.scheduler`), and Go CLI verbs
(``cli/internal/cmd/scheduler``) all dispatch through, so the tenant
boundary, the FK check, and the audit contract are enforced in one
place.

Concurrency model
-----------------

Stateless and method-scoped, mirroring
:class:`~meho_backplane.agents.service.AgentDefinitionService`: each
public method opens its own :class:`~sqlalchemy.ext.asyncio.AsyncSession`
via :func:`~meho_backplane.db.engine.get_sessionmaker`, commits, and
closes. No shared transaction state across calls.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter -- the
boundary derives it from the operator's JWT (or, for a ``tenant_admin``
caller targeting another tenant, from the request body / query string).
Every query starts with ``WHERE tenant_id = :tenant_id`` so
cross-tenant rows are structurally invisible: a ``get`` / ``cancel``
against another tenant's trigger returns ``None`` / ``False`` (the 404
the route renders), never the other tenant's row.

RBAC
----

This service does **not** enforce roles -- it assumes the caller has
already validated the tenant role (``tenant_admin`` for create /
cancel, ``operator`` for list). The REST routes / MCP tools / CLI verbs
own the :func:`~meho_backplane.auth.rbac.require_role` gate.

Error contract
--------------

* :class:`AgentDefinitionMissingError` -- the create body's
  ``agent_definition_id`` does not name an agent definition in
  *tenant_id*. Mapped to 422 at the REST boundary.
* List / get / cancel signal *absence* via ``None`` / ``False`` rather
  than an exception, so the 404-vs-existence-leak collapse stays
  trivial at the boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    ScheduledTrigger,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
)
from meho_backplane.scheduler.repository import (
    create_cron_trigger,
    create_event_trigger,
    create_one_off_trigger,
)
from meho_backplane.scheduler.schemas import (
    ScheduledTriggerCreate,
    ScheduledTriggerRead,
)

__all__ = [
    "AgentDefinitionMissingError",
    "SchedulerAdminService",
]


#: Default per-call paging cap for :meth:`SchedulerAdminService.list_`.
#: Trigger corpora per tenant are anticipated to be "dozens" per the
#: consumer doc; 100 covers the steady-state case in one page and the
#: cap stays in place so a runaway tenant doesn't stream the whole
#: table back to a casual ``meho scheduler list``.
DEFAULT_LIST_LIMIT: int = 100

#: Hard upper bound on the paging cap. Mirrors the agents service so
#: an operator scripting a one-shot bulk fetch can pass --limit 500
#: without recompiling.
MAX_LIST_LIMIT: int = 500


class AgentDefinitionMissingError(Exception):
    """Raised when ``agent_definition_id`` does not resolve in this tenant.

    Distinct from a generic FK violation: this exception fires after
    a pre-flight SELECT against ``agent_definition`` returned no row,
    which means the caller's id is either typo'd, deleted, or belongs
    to another tenant. The REST route maps it to 422
    ``agent_definition_not_found``; the MCP tool to an invalid-params
    error with the same code.
    """

    def __init__(self, agent_definition_id: uuid.UUID) -> None:
        self.agent_definition_id = agent_definition_id
        super().__init__(
            f"agent definition {agent_definition_id} not found in this tenant",
        )


def _row_to_read(row: ScheduledTrigger) -> ScheduledTriggerRead:
    """Materialise a :class:`ScheduledTrigger` ORM row as the wire shape.

    The ORM stores ``DateTime(timezone=True)`` columns as naive on
    aiosqlite (the unit-test path) and aware on PG. The schema's
    :class:`~pydantic.BaseModel` accepts both; the consumer (REST
    JSON renderer / MCP wire-shape dump) normalises to ISO 8601 with
    or without an explicit offset accordingly. We do **not** force-
    attach UTC here: doing so would lie about timezone on the SQLite
    path. The integration test path runs against PG where the values
    are aware end-to-end.
    """
    return ScheduledTriggerRead.model_validate(row, from_attributes=True)


class SchedulerAdminService:
    """Tenant-scoped CRUD over :class:`ScheduledTrigger`.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session, commits, and closes -- no shared
    transaction state across calls.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def _assert_agent_definition_exists(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        agent_definition_id: uuid.UUID,
    ) -> None:
        """Reject *agent_definition_id* unless it names a row in *tenant_id*.

        Pre-flight inside the caller's session so the check and the
        downstream INSERT share one transaction; a concurrent delete
        between the SELECT and the INSERT would surface as the DB-side
        FK :class:`IntegrityError` which the create method translates
        back into :class:`AgentDefinitionMissingError`.

        The query selects ``1`` (the cheapest projection) -- the row
        contents are irrelevant; only existence matters.
        """
        result = await session.execute(
            select(AgentDefinition.id).where(
                AgentDefinition.id == agent_definition_id,
                AgentDefinition.tenant_id == tenant_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise AgentDefinitionMissingError(agent_definition_id)

    async def create(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: ScheduledTriggerCreate,
    ) -> ScheduledTriggerRead:
        """Create one scheduled trigger under *tenant_id*.

        Dispatches to the kind-specific repository helper based on
        :attr:`ScheduledTriggerCreate.kind`. The discriminated-union
        validation already ran at the Pydantic layer; this method only
        re-checks the FK to surface a clean 422 (rather than a 500 on
        :class:`IntegrityError`).

        Raises
        ------
        AgentDefinitionMissingError
            When ``agent_definition_id`` does not name a definition in
            *tenant_id*. The boundary maps this to 422
            ``agent_definition_not_found``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await self._assert_agent_definition_exists(
                session,
                tenant_id,
                payload.agent_definition_id,
            )
            try:
                if payload.kind == ScheduledTriggerKind.CRON:
                    # The Pydantic validator already proved cron_expr is
                    # non-null; assert to give mypy a narrow type.
                    assert payload.cron_expr is not None
                    row = await create_cron_trigger(
                        session,
                        tenant_id=tenant_id,
                        agent_definition_id=payload.agent_definition_id,
                        cron_expr=payload.cron_expr,
                        timezone=payload.timezone,
                        inputs=payload.inputs,
                        identity_sub=payload.identity_sub,
                        created_by_sub=created_by_sub,
                        in_flight_policy=payload.in_flight_policy.value,
                        work_ref=payload.work_ref,
                    )
                elif payload.kind == ScheduledTriggerKind.ONE_OFF:
                    assert payload.fire_at is not None
                    row = await create_one_off_trigger(
                        session,
                        tenant_id=tenant_id,
                        agent_definition_id=payload.agent_definition_id,
                        run_at=payload.fire_at,
                        inputs=payload.inputs,
                        identity_sub=payload.identity_sub,
                        created_by_sub=created_by_sub,
                        in_flight_policy=payload.in_flight_policy.value,
                        work_ref=payload.work_ref,
                    )
                else:  # payload.kind == ScheduledTriggerKind.EVENT
                    assert payload.event_filter is not None
                    row = await create_event_trigger(
                        session,
                        tenant_id=tenant_id,
                        agent_definition_id=payload.agent_definition_id,
                        event_filter=payload.event_filter,
                        inputs=payload.inputs,
                        identity_sub=payload.identity_sub,
                        created_by_sub=created_by_sub,
                        in_flight_policy=payload.in_flight_policy.value,
                        work_ref=payload.work_ref,
                    )
                await session.commit()
            except IntegrityError as exc:
                # A race between the pre-flight FK check and the INSERT
                # (the definition got deleted in between) lands here.
                await session.rollback()
                raise AgentDefinitionMissingError(
                    payload.agent_definition_id,
                ) from exc
            await session.refresh(row)
            return _row_to_read(row)

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        kind: str | None = None,
        status: str | None = None,
        work_ref: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> Sequence[ScheduledTriggerRead]:
        """List triggers under *tenant_id*; newest-first.

        Optional *kind* / *status* filters narrow the result by the
        respective columns. The route layer is the one that validates
        the values against the closed enums (the schema's
        :data:`KindFilter` / :data:`StatusFilter` literals); this
        method accepts the raw string so a future widening doesn't
        require lock-step changes.

        Optional *work_ref* narrows to triggers carrying that exact
        change-ticket reference (work_ref I3-T3 #1663) -- the
        tenant-scoped exact-match driven by
        ``scheduled_trigger_tenant_work_ref_idx``. ``None`` (the
        default) applies no work_ref filter.

        Ordering is ``created_at DESC`` so the operator sees the most
        recent trigger first -- matching the precedent
        :class:`~meho_backplane.agents.service.AgentDefinitionService`
        sets, and intuitive when an operator just created a trigger
        and wants to confirm it landed.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(ScheduledTrigger)
                .where(ScheduledTrigger.tenant_id == tenant_id)
                .order_by(ScheduledTrigger.created_at.desc(), ScheduledTrigger.id)
                .limit(limit)
                .offset(offset)
            )
            if kind is not None:
                stmt = stmt.where(ScheduledTrigger.kind == kind)
            if status is not None:
                stmt = stmt.where(ScheduledTrigger.status == status)
            if work_ref is not None:
                stmt = stmt.where(ScheduledTrigger.work_ref == work_ref)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            return [_row_to_read(r) for r in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        trigger_id: uuid.UUID,
    ) -> ScheduledTriggerRead | None:
        """Return one trigger by id; ``None`` on absence / cross-tenant.

        The tenant filter is the first WHERE clause so a probe for
        another tenant's trigger id surfaces as ``None`` (404 at the
        boundary), never as the other tenant's row.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(ScheduledTrigger).where(
                ScheduledTrigger.tenant_id == tenant_id,
                ScheduledTrigger.id == trigger_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_read(row)

    async def _read_status(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        trigger_id: uuid.UUID,
    ) -> str | None:
        """Return the trigger's current ``status`` column value or ``None``.

        Tenant-scoped: a probe for another tenant's id returns
        ``None`` (same 404-vs-existence-leak collapse the boundary
        relies on).
        """
        result = await session.execute(
            select(ScheduledTrigger.status).where(
                ScheduledTrigger.tenant_id == tenant_id,
                ScheduledTrigger.id == trigger_id,
            )
        )
        return result.scalar_one_or_none()

    async def _conditional_cancel_update(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        trigger_id: uuid.UUID,
    ) -> int:
        """Conditional ``status=cancelled`` UPDATE; return rowcount.

        Matches the active / paused set so a concurrent fire (status
        advanced to ``fired``) or a concurrent cancel (status already
        ``cancelled``) safely surfaces as rowcount==0. The caller
        re-reads and disambiguates.
        """
        stmt = (
            update(ScheduledTrigger)
            .where(
                ScheduledTrigger.id == trigger_id,
                ScheduledTrigger.tenant_id == tenant_id,
                ScheduledTrigger.status.in_(
                    [
                        ScheduledTriggerStatus.ACTIVE.value,
                        ScheduledTriggerStatus.PAUSED.value,
                    ]
                ),
            )
            .values(
                status=ScheduledTriggerStatus.CANCELLED.value,
                updated_at=datetime.now(UTC),
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def cancel(
        self,
        tenant_id: uuid.UUID,
        trigger_id: uuid.UUID,
    ) -> bool:
        """Transition a trigger to ``status='cancelled'``; return ``True`` on success.

        The transition is **terminal** (per :class:`ScheduledTriggerStatus`'s
        docstring): a cancelled trigger never fires again. The row is
        retained for audit.

        Idempotent shape: cancelling an already-cancelled trigger
        returns ``True``. A trigger that hit terminal ``fired`` is
        **not** cancellable; returns ``False`` and the boundary maps
        it to 409 ``trigger_already_fired``. Cross-tenant or absent
        trigger -> ``False`` (404 at boundary).

        Concurrency contract (TOCTOU-safe)
        ----------------------------------

        The conditional UPDATE (``WHERE status IN (active, paused)``)
        lets two concurrent cancel callers race safely. The first wins
        (rowcount==1). The loser's UPDATE returns rowcount==0, and a
        naive read of the pre-flight SELECT (which saw ``active``)
        would mis-classify the loser's outcome as
        ``trigger_already_fired`` (the phantom 409 in review B1 on
        PR #1128).

        The fix is a **read-after-update**: when rowcount==0, re-read
        the row's *current* status and disambiguate:

        * ``CANCELLED`` -> other caller won the cancel race. Return
          ``True`` (idempotent success).
        * ``FIRED`` -> terminal one-off; ``False`` (409).
        * Row gone -> ``False`` (404).
        * Any other status -> conservative ``False``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            # Pre-flight: classify obviously-already-terminal states
            # before the conditional UPDATE so we don't waste a write
            # on the cancelled / fired / absent rows.
            current_status = await self._read_status(session, tenant_id, trigger_id)
            if current_status is None:
                return False
            if current_status == ScheduledTriggerStatus.CANCELLED.value:
                return True
            if current_status == ScheduledTriggerStatus.FIRED.value:
                return False
            # active / paused -> cancelled (conditional).
            rowcount = await self._conditional_cancel_update(session, tenant_id, trigger_id)
            if rowcount > 0:
                return True
            # Read-after-update: rowcount==0 means another writer
            # transitioned the row between our pre-flight SELECT and
            # our UPDATE. Re-read to disambiguate idempotent success
            # (CANCELLED) from real failure (FIRED / gone / etc).
            post_race_status = await self._read_status(session, tenant_id, trigger_id)
            return post_race_status == ScheduledTriggerStatus.CANCELLED.value
