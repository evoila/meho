# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``CheckDashboardAdminService`` -- tenant-scoped CRUD + read-time rollup (#2506).

Task #2506 under Initiative #2416 (parent goal #221). The single code path
the REST routes (:mod:`meho_backplane.api.v1.checks_dashboards`) and the
``/ui/checks`` console call through, so the tenant boundary and the
membership-validation guard are enforced in one place. Mirrors
:class:`~meho_backplane.checks.service.SensorAdminService`.

The serving rollup is **evaluated on read** (decision on #2506): ``get`` and
``list_`` fold each Dashboard's members through
:mod:`meho_backplane.checks.rollup` at request time against ``now``. Nothing
here writes ``check_dashboards.last_rollup_state`` -- that memo column is
#2507's, shipped unwritten by this Task.

Concurrency model
-----------------

Stateless and method-scoped: each public method opens its own
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, commits (for writes), and
closes. No shared transaction state across calls.

RBAC
----

This service does **not** enforce roles -- the REST routes / UI routes own
the :func:`~meho_backplane.auth.rbac.require_role` gate and the
:func:`~meho_backplane.auth.rbac.authorize_tenant_scope` cross-tenant gate
(``operator`` for list / get, ``tenant_admin`` for create / delete, and
``platform_admin`` for any explicit cross-tenant ``tenant_id``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy.exc import IntegrityError

from meho_backplane.checks.assertions import CheckState
from meho_backplane.checks.dashboard_repository import (
    create_dashboard,
    delete_dashboard,
    existing_sensor_ids,
    get_dashboard,
    list_dashboards,
    members_by_dashboard,
)
from meho_backplane.checks.dashboard_schemas import (
    DashboardCreate,
    DashboardDetail,
    DashboardMemberView,
    DashboardRead,
)
from meho_backplane.checks.rollup import (
    MemberEvaluation,
    MemberState,
    evaluate_member,
    fold,
)
from meho_backplane.checks.service import _is_unique_violation
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard, Sensor, SensorSeverity, SensorStatus

__all__ = [
    "CheckDashboardAdminService",
    "DashboardNameConflictError",
    "SensorNotFoundError",
]

#: Default per-call paging cap for :meth:`CheckDashboardAdminService.list_`.
DEFAULT_LIST_LIMIT: int = 100

#: Hard upper bound on the paging cap (mirrors the Sensor admin service).
MAX_LIST_LIMIT: int = 500


class SensorNotFoundError(Exception):
    """Raised when a create references a sensor id absent from the tenant.

    Every id in ``sensor_ids`` must resolve to a Sensor under the target
    tenant; a foreign or absent id is refused before any write. The boundary
    maps this to 422 ``sensor_not_found``.
    """

    #: Machine-readable error code the boundary surfaces on every transport.
    error_code = "sensor_not_found"

    def __init__(self, missing: Sequence[uuid.UUID]) -> None:
        self.missing = list(missing)
        joined = ", ".join(str(m) for m in self.missing)
        super().__init__(f"sensor id(s) not found in this tenant: {joined}")


class DashboardNameConflictError(Exception):
    """Raised when the ``(tenant_id, name)`` pair is already taken.

    Dashboard names are unique per tenant (the
    ``check_dashboard_tenant_name_idx`` unique index). A duplicate surfaces
    here after the flush-time :class:`~sqlalchemy.exc.IntegrityError`; the
    boundary maps it to 409 ``dashboard_name_conflict``.
    """

    #: Machine-readable error code surfaced on every transport.
    error_code = "dashboard_name_conflict"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"a dashboard named {name!r} already exists in this tenant")


def _member_state(sensor: Sensor) -> MemberState:
    """Project a ``sensor`` row into the fold-relevant :class:`MemberState`.

    ``last_state`` is CHECK-constrained to the five-state vocabulary at the
    DB, so the narrowing cast is honest.
    """
    return MemberState(
        last_state=cast(CheckState, sensor.last_state),
        status=sensor.status,
        severity=sensor.severity,
        for_seconds=sensor.for_seconds,
        last_evaluated_at=sensor.last_evaluated_at,
        next_fire_at=sensor.next_fire_at,
        state_since=sensor.state_since,
    )


def _member_view(sensor: Sensor, evaluation: MemberEvaluation) -> DashboardMemberView:
    """Assemble one member's detail-row view from its sensor + evaluation."""
    return DashboardMemberView(
        sensor_id=sensor.id,
        name=sensor.name,
        connector_id=sensor.connector_id,
        op_id=sensor.op_id,
        raw_state=evaluation.raw_state,
        effective_state=evaluation.effective_state,
        pending=evaluation.pending,
        severity=SensorSeverity(sensor.severity),
        for_seconds=sensor.for_seconds,
        status=SensorStatus(sensor.status),
        state_since=sensor.state_since,
        last_value=sensor.last_value,
        last_evidence=sensor.last_evidence,
        last_evaluated_at=sensor.last_evaluated_at,
        next_fire_at=sensor.next_fire_at,
    )


def _to_read(row: CheckDashboard, sensors: Sequence[Sensor], now: datetime) -> DashboardRead:
    """Build the list-row view (rolled-up state + member count)."""
    state = fold([evaluate_member(_member_state(s), now) for s in sensors])
    return DashboardRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        member_count=len(sensors),
        state=state,
        last_rollup_state=cast("CheckState | None", row.last_rollup_state),
        created_by_sub=row.created_by_sub,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_detail(row: CheckDashboard, sensors: Sequence[Sensor], now: datetime) -> DashboardDetail:
    """Build the detail view (rolled-up state + per-member breakdown)."""
    evaluations = [evaluate_member(_member_state(s), now) for s in sensors]
    state = fold(evaluations)
    members = [_member_view(s, e) for s, e in zip(sensors, evaluations, strict=True)]
    return DashboardDetail(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        member_count=len(sensors),
        state=state,
        last_rollup_state=cast("CheckState | None", row.last_rollup_state),
        created_by_sub=row.created_by_sub,
        created_at=row.created_at,
        updated_at=row.updated_at,
        members=members,
    )


class CheckDashboardAdminService:
    """Tenant-scoped CRUD over :class:`CheckDashboard` with a read-time rollup.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session -- no shared transaction state.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger(__name__)

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: DashboardCreate,
        now: datetime | None = None,
    ) -> DashboardDetail:
        """Create one Dashboard under *tenant_id*; return its detail view.

        Validates every ``payload.sensor_ids`` entry against the tenant's
        Sensors (a foreign / absent id -> :class:`SensorNotFoundError`),
        de-duplicates the membership list, inserts the Dashboard + memberships
        in one transaction, and returns the freshly rolled-up detail.

        Raises:
            SensorNotFoundError: a referenced sensor id is not in the tenant.
            DashboardNameConflictError: ``(tenant_id, name)`` is taken.
        """
        now = now or datetime.now(UTC)
        # De-duplicate while preserving order so a body listing the same
        # sensor twice does not trip the composite PK.
        deduped = list(dict.fromkeys(payload.sensor_ids))
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            if deduped:
                present = await existing_sensor_ids(
                    session, tenant_id=tenant_id, sensor_ids=deduped
                )
                missing = [sid for sid in deduped if sid not in present]
                if missing:
                    raise SensorNotFoundError(missing)
            try:
                row = await create_dashboard(
                    session,
                    tenant_id=tenant_id,
                    name=payload.name,
                    description=payload.description,
                    sensor_ids=deduped,
                    created_by_sub=created_by_sub,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if _is_unique_violation(exc):
                    raise DashboardNameConflictError(payload.name) from exc
                raise
            sensors = (await members_by_dashboard(session, dashboard_ids=[row.id])).get(row.id, [])
            return _to_detail(row, sensors, now)

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        now: datetime | None = None,
    ) -> Sequence[DashboardRead]:
        """List Dashboards under *tenant_id* (newest-first) with rolled-up state.

        Members for every listed Dashboard are loaded in one join query (no
        per-row N+1); each row's rollup is folded on read against *now*.
        """
        now = now or datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = await list_dashboards(session, tenant_id=tenant_id, limit=limit, offset=offset)
            grouped = await members_by_dashboard(session, dashboard_ids=[r.id for r in rows])
            return [_to_read(r, grouped.get(r.id, []), now) for r in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        dashboard_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> DashboardDetail | None:
        """Return one Dashboard's detail by id; ``None`` on absence / cross-tenant.

        The tenant filter is the first WHERE clause so a probe for another
        tenant's dashboard id surfaces as ``None`` (404 at the boundary).
        """
        now = now or datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await get_dashboard(session, tenant_id=tenant_id, dashboard_id=dashboard_id)
            if row is None:
                return None
            sensors = (await members_by_dashboard(session, dashboard_ids=[row.id])).get(row.id, [])
            return _to_detail(row, sensors, now)

    async def delete(self, tenant_id: uuid.UUID, dashboard_id: uuid.UUID) -> bool:
        """Delete a Dashboard by id; return ``True`` when a row was removed.

        Tenant-scoped: a cross-tenant / absent id removes nothing and returns
        ``False`` (the 404 the boundary renders). Memberships are removed
        with the Dashboard (explicit delete -- dialect-safe).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            deleted = await delete_dashboard(
                session, tenant_id=tenant_id, dashboard_id=dashboard_id
            )
            await session.commit()
            return deleted
