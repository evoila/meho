# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``SensorAdminService`` -- tenant-scoped CRUD over the ``sensor`` table.

Task #2503 under Initiative #2416 (parent goal #221). The single code
path the REST routes (:mod:`meho_backplane.api.v1.sensors`), MCP verbs
(:mod:`meho_backplane.mcp.tools.sensors`), and Go CLI verbs
(``cli/internal/cmd/sensor``) all dispatch through, so the tenant
boundary, the safe-only create guard, and the audit contract are enforced
in one place. Mirrors :class:`~meho_backplane.scheduler.service.SchedulerAdminService`.

Concurrency model
-----------------

Stateless and method-scoped: each public method opens its own
:class:`~sqlalchemy.ext.asyncio.AsyncSession` via
:func:`~meho_backplane.db.engine.get_sessionmaker`, commits, and closes.
No shared transaction state across calls.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter and every
query starts with ``WHERE tenant_id = :tenant_id`` so cross-tenant rows
are structurally invisible: a ``get`` / ``delete`` against another
tenant's sensor returns ``None`` / ``False`` (the 404 the route renders),
never the other tenant's row.

RBAC
----

This service does **not** enforce roles -- the REST routes / MCP tools /
CLI verbs own the :func:`~meho_backplane.auth.rbac.require_role` gate
(``operator`` for list, ``tenant_admin`` for create / delete).

Error contract
--------------

* :class:`SensorOperationNotFoundError` -- ``(connector_id, op_id)`` does
  not resolve to a descriptor visible to *tenant_id*. Mapped to 422.
* :class:`SensorRequiresSafeOperationError` -- the resolved descriptor's
  ``safety_level`` is not ``safe``. Mapped to 422.
* :class:`SensorNameConflictError` -- the ``(tenant_id, name)`` pair is
  already taken. Mapped to 409.
* List / get / delete signal *absence* via ``None`` / ``False`` rather
  than an exception, so the 404-vs-existence-leak collapse stays trivial
  at the boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from meho_backplane.checks.repository import create_sensor
from meho_backplane.checks.schemas import SensorCreate, SensorRead
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor
from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

__all__ = [
    "SensorAdminService",
    "SensorNameConflictError",
    "SensorOperationNotFoundError",
    "SensorRequiresSafeOperationError",
]


#: Default per-call paging cap for :meth:`SensorAdminService.list_`.
#: Mirrors the scheduler admin service.
DEFAULT_LIST_LIMIT: int = 100

#: Hard upper bound on the paging cap. Mirrors the scheduler admin service
#: so an operator scripting a bulk fetch can pass ``--limit 500``.
MAX_LIST_LIMIT: int = 500


class SensorOperationNotFoundError(Exception):
    """Raised when ``(connector_id, op_id)`` resolves to no descriptor.

    The ``connector_id`` is parsed into ``(product, version, impl_id)``
    and, with ``op_id``, looked up against ``endpoint_descriptor`` (tenant
    -scoped then global). No enabled descriptor -> the op is unknown or
    disabled; the boundary maps this to 422 ``sensor_operation_not_found``.
    """

    #: Machine-readable error code the boundary surfaces on every transport
    #: so a caller branches on the string, not on prose.
    error_code = "sensor_operation_not_found"

    def __init__(self, connector_id: str, op_id: str) -> None:
        self.connector_id = connector_id
        self.op_id = op_id
        super().__init__(
            f"no enabled operation {op_id!r} on connector {connector_id!r} "
            "is visible to this tenant",
        )


class SensorRequiresSafeOperationError(Exception):
    """Raised when a Sensor's op is not ``safety_level='safe'`` (#2503).

    A Sensor registration accepts only operations whose ``safety_level``
    is ``safe``: a Sensor is evaluated unattended on a schedule, so
    binding a ``caution`` / ``dangerous`` op to it would run a
    side-effecting or destructive op without a human in the loop. Refused
    at create; the boundary maps it to 422 ``sensor_requires_safe_operation``
    (the MCP transport surfaces the same code as an invalid-params error).

    This is a **create-time honesty guard, not the security boundary**:
    the dispatch-time policy gate
    (:func:`meho_backplane.operations.dispatcher.dispatch`) still runs on
    every #2505 evaluation, so a descriptor whose ``safety_level`` is later
    re-ingested harder fails closed at dispatch even for an already-created
    sensor.
    """

    #: Machine-readable error code surfaced on every transport.
    error_code = "sensor_requires_safe_operation"

    def __init__(self, connector_id: str, op_id: str, safety_level: str) -> None:
        self.connector_id = connector_id
        self.op_id = op_id
        self.safety_level = safety_level
        super().__init__(
            f"operation {op_id!r} on connector {connector_id!r} has "
            f"safety_level={safety_level!r}; a Sensor may reference only "
            "safe operations (it is evaluated unattended)",
        )


class SensorNameConflictError(Exception):
    """Raised when the ``(tenant_id, name)`` pair is already taken.

    Sensors are referenced by name from Dashboards (#2506), so the name is
    unique per tenant (the ``sensor_tenant_name_idx`` unique index). A
    duplicate surfaces here after the flush-time
    :class:`~sqlalchemy.exc.IntegrityError`; the boundary maps it to 409
    ``sensor_name_conflict``.
    """

    #: Machine-readable error code surfaced on every transport.
    error_code = "sensor_name_conflict"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"a sensor named {name!r} already exists in this tenant")


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether *exc* is a unique-constraint violation.

    Mirrors :func:`meho_backplane.agents.service._is_unique_violation` and
    the convention service: PG (asyncpg) exposes the SQLSTATE via
    ``orig.sqlstate`` -- ``23505`` is ``unique_violation``; SQLite emits the
    documented ``UNIQUE constraint failed`` substring. The ``pgcode``
    fallback survives a future psycopg wiring.

    Narrowing matters here because :meth:`SensorAdminService.create` can pass
    a cross-tenant ``tenant_id`` (platform-admin path): a bogus one trips the
    tenant FK, and a future tightening migration could add a CHECK. Those are
    genuine integrity failures, not a duplicate name -- returning ``False``
    lets them propagate as a 500 rather than a misleading 409
    ``sensor_name_conflict``.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    orig_msg = str(orig or exc)
    return sqlstate == "23505" or "UNIQUE constraint failed" in orig_msg


def _row_to_read(row: Sensor) -> SensorRead:
    """Materialise a :class:`Sensor` ORM row as the wire shape.

    The ORM stores ``DateTime(timezone=True)`` columns as naive on
    aiosqlite (the unit-test path) and aware on PG; the schema accepts
    both. We do **not** force-attach UTC here -- that would lie about
    timezone on the SQLite path.
    """
    return SensorRead.model_validate(row, from_attributes=True)


class SensorAdminService:
    """Tenant-scoped CRUD over :class:`Sensor`.

    Stateless and async; instantiate once and call freely. Each public
    method opens its own DB session, commits, and closes -- no shared
    transaction state across calls.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger(__name__)

    async def create(
        self,
        tenant_id: uuid.UUID,
        created_by_sub: str,
        payload: SensorCreate,
    ) -> SensorRead:
        """Create one sensor under *tenant_id*.

        Resolves ``(connector_id, op_id)`` to an
        :class:`~meho_backplane.db.models.EndpointDescriptor` and refuses
        the create unless the descriptor exists and is ``safety_level='safe'``
        (the safe-only guard). The assertion payload was already validated
        at the Pydantic layer (parsed into
        :class:`~meho_backplane.checks.assertions.AssertionSpec`); this
        method stores its serialised form.

        Raises
        ------
        SensorOperationNotFoundError
            ``(connector_id, op_id)`` resolves to no enabled descriptor.
            The boundary maps this to 422 ``sensor_operation_not_found``.
        SensorRequiresSafeOperationError
            The descriptor's ``safety_level`` is not ``safe``. The boundary
            maps this to 422 ``sensor_requires_safe_operation``.
        SensorNameConflictError
            The ``(tenant_id, name)`` pair is already taken. The boundary
            maps this to 409 ``sensor_name_conflict``.
        sqlalchemy.exc.IntegrityError
            Any *other* integrity failure (a tenant-FK violation from a
            bogus cross-tenant ``tenant_id``, a CHECK from a future
            tightening migration) propagates rather than being misreported
            as a name conflict -- the boundary maps it to a 500.
        """
        # Safe-only create guard -- resolve the descriptor and refuse a
        # non-safe / unknown op before any DB write.
        product, version, impl_id = parse_connector_id(payload.connector_id)
        descriptor = await lookup_descriptor(
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=payload.op_id,
        )
        if descriptor is None:
            raise SensorOperationNotFoundError(payload.connector_id, payload.op_id)
        if descriptor.safety_level != "safe":
            raise SensorRequiresSafeOperationError(
                payload.connector_id,
                payload.op_id,
                descriptor.safety_level,
            )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            try:
                row = await create_sensor(
                    session,
                    tenant_id=tenant_id,
                    name=payload.name,
                    connector_id=payload.connector_id,
                    op_id=payload.op_id,
                    target=payload.target,
                    params=payload.params,
                    assertion=payload.assertion.model_dump(mode="json"),
                    cadence_kind=payload.cadence_kind,
                    interval_seconds=payload.interval_seconds,
                    cron_expr=payload.cron_expr,
                    timezone=payload.timezone,
                    severity=payload.severity.value,
                    for_seconds=payload.for_seconds,
                    identity_sub=payload.identity_sub,
                    created_by_sub=created_by_sub,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if _is_unique_violation(exc):
                    # The unique (tenant_id, name) index rejected a duplicate.
                    raise SensorNameConflictError(payload.name) from exc
                # Any other integrity failure (a tenant-FK violation from a
                # bogus cross-tenant tenant_id, a CHECK from a future
                # tightening migration) is not a name conflict -- re-raise so
                # it surfaces as a 500 rather than a misleading 409.
                raise
            await session.refresh(row)
            return _row_to_read(row)

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        status: str | None = None,
        cadence_kind: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> Sequence[SensorRead]:
        """List sensors under *tenant_id*; newest-first.

        Optional *status* / *cadence_kind* filters narrow the result by the
        respective columns. The route layer validates the values against
        the closed enums; this method accepts the raw string so a future
        widening does not require lock-step changes.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(Sensor)
                .where(Sensor.tenant_id == tenant_id)
                .order_by(Sensor.created_at.desc(), Sensor.id)
                .limit(limit)
                .offset(offset)
            )
            if status is not None:
                stmt = stmt.where(Sensor.status == status)
            if cadence_kind is not None:
                stmt = stmt.where(Sensor.cadence_kind == cadence_kind)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            return [_row_to_read(r) for r in rows]

    async def get(
        self,
        tenant_id: uuid.UUID,
        sensor_id: uuid.UUID,
    ) -> SensorRead | None:
        """Return one sensor by id; ``None`` on absence / cross-tenant.

        The tenant filter is the first WHERE clause so a probe for another
        tenant's sensor id surfaces as ``None`` (404 at the boundary),
        never as the other tenant's row.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(Sensor).where(
                Sensor.tenant_id == tenant_id,
                Sensor.id == sensor_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_read(row)

    async def delete(
        self,
        tenant_id: uuid.UUID,
        sensor_id: uuid.UUID,
    ) -> bool:
        """Hard-delete a sensor by id; return ``True`` when a row was removed.

        Unlike trigger cancel-and-retain, a sensor row carries no
        fire-history the audit trail needs post-delete (create / delete are
        audited via the ``audit_op_id`` contextvar), so the delete is a
        hard ``DELETE``. Tenant-scoped: a cross-tenant / absent id removes
        nothing and returns ``False`` (the 404 the boundary renders).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = delete(Sensor).where(
                Sensor.tenant_id == tenant_id,
                Sensor.id == sensor_id,
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount) > 0  # type: ignore[attr-defined]
