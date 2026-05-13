# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/targets`` — CRUD surface for the targets registry.

5 routes (G0.3-T3 / Task #254):

* ``GET  /api/v1/targets``           — list, keyset-paginated. ``operator`` role.
* ``GET  /api/v1/targets/{name}``    — describe (alias-aware). ``operator`` role.
* ``POST /api/v1/targets/{name}/probe`` — invoke connector probe. ``operator`` role.
* ``POST /api/v1/targets``           — create. ``tenant_admin`` role.
* ``PATCH /api/v1/targets/{name}``   — update (partial). ``tenant_admin`` role.

All routes are tenant-scoped via ``operator.tenant_id`` extracted from the
JWT by :func:`~meho_backplane.middleware.verify_jwt_and_bind`. Cross-tenant
reads are impossible — the WHERE clause always includes ``tenant_id``.

Alias resolution
----------------

``GET /{name}`` and ``PATCH /{name}`` both pass the caller-supplied ``name``
to :func:`~meho_backplane.targets.resolver.resolve_target`, which implements
the 3-step algorithm: exact name → alias element-equality → near-miss 404.
Callers can address a target by any of its aliases and get the same result.

Audit enrichment
----------------

Every route that successfully calls ``resolve_target`` (or creates a target)
binds ``audit_target_id`` into structlog contextvars. G0.3-T4 (#255) will
extend :class:`~meho_backplane.audit.AuditMiddleware` to read that contextvar
and populate ``audit_log.target_id``.

Probe route
-----------

The probe route delegates to the product's registered
:class:`~meho_backplane.connectors.base.Connector`. If no connector is
registered for the target's product (connector not yet implemented, or G0.2
not fully landed), the route returns 501. The target must exist for the probe
to fire — a non-existent target returns 404 via ``resolve_target``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors.registry import get_connector
from meho_backplane.connectors.schemas import AuthModel, ProbeResult
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.schemas import Target, TargetCreate, TargetSummary, TargetUpdate

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])

#: Module-level Depends closures — required to satisfy ruff B008 (mutable
#: calls in default argument positions are disallowed). Pattern matches
#: :mod:`meho_backplane.api.v1.retrieve`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


def _to_summary(t: TargetORM) -> TargetSummary:
    return TargetSummary(
        id=t.id,
        name=t.name,
        aliases=t.aliases,
        product=t.product,
        host=t.host,
    )


def _to_full(t: TargetORM) -> Target:
    return Target(
        id=t.id,
        tenant_id=t.tenant_id,
        name=t.name,
        aliases=t.aliases,
        product=t.product,
        host=t.host,
        port=t.port,
        fqdn=t.fqdn,
        secret_ref=t.secret_ref,
        auth_model=AuthModel(t.auth_model),
        vpn_required=t.vpn_required,
        extras=t.extras,
        notes=t.notes,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.get("", response_model=list[TargetSummary])
async def list_targets(
    product: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> list[TargetSummary]:
    """List targets for the requesting tenant.

    Results are keyset-paginated by ``name`` (lexicographic order).
    Pass ``cursor=<last-name-seen>`` to fetch the next page. The
    ``product`` filter is exact-match; pass it to narrow by product
    slug. ``limit`` defaults to 100, max 500.
    """
    stmt = select(TargetORM).where(TargetORM.tenant_id == operator.tenant_id)
    if product is not None:
        stmt = stmt.where(TargetORM.product == product)
    if cursor is not None:
        stmt = stmt.where(TargetORM.name > cursor)
    stmt = stmt.order_by(TargetORM.name).limit(limit)
    result = await session.execute(stmt)
    return [_to_summary(t) for t in result.scalars().all()]


@router.get("/{name}", response_model=Target)
async def describe_target(
    name: str,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> Target:
    """Describe a target by name or alias.

    Uses :func:`~meho_backplane.targets.resolver.resolve_target` so
    callers can pass any alias instead of the canonical name. Returns
    404 with near-misses when nothing matches.
    """
    t = await resolve_target(session, operator.tenant_id, name)
    structlog.contextvars.bind_contextvars(audit_target_id=str(t.id))
    return _to_full(t)


@router.post("/{name}/probe", response_model=ProbeResult)
async def probe_target(
    name: str,
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> ProbeResult:
    """Invoke the registered connector's ``probe`` method for a target.

    Returns 501 when no connector is registered for the target's
    product slug. The connector is free to return a failed probe
    (``ok=False``) with a reason; that is still a 200 response —
    the route succeeded, the connector reported the target as
    unreachable.
    """
    t = await resolve_target(session, operator.tenant_id, name)
    structlog.contextvars.bind_contextvars(audit_target_id=str(t.id))
    cls = get_connector(t.product)
    if cls is None:
        raise HTTPException(
            status_code=501,
            detail=f"no connector registered for product={t.product!r}",
        )
    return await cls().probe(t)


@router.post("", response_model=Target, status_code=201)
async def create_target(
    body: TargetCreate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Target:
    """Create a new target in the requesting tenant.

    Returns 409 when a target with the same ``name`` already exists in
    the tenant. The ``id`` and timestamps are generated server-side.
    ``tenant_id`` is always taken from the JWT — the body cannot override
    it.
    """
    existing = await session.execute(
        select(TargetORM).where(
            TargetORM.tenant_id == operator.tenant_id,
            TargetORM.name == body.name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"target {body.name!r} already exists in tenant",
        )
    now = datetime.now(UTC)
    t = TargetORM(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        created_at=now,
        updated_at=now,
        **body.model_dump(),
    )
    session.add(t)
    structlog.contextvars.bind_contextvars(audit_target_id=str(t.id))
    _log.info(
        "target_created",
        target_id=str(t.id),
        name=t.name,
        tenant_id=str(operator.tenant_id),
    )
    return _to_full(t)


@router.patch("/{name}", response_model=Target)
async def update_target(
    name: str,
    body: TargetUpdate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Target:
    """Partially update a target.

    Only fields present in the request body are modified (Pydantic
    ``exclude_unset``). ``name`` and ``product`` are not patchable —
    rename a target by deleting and re-creating it (v0.2 decision).
    ``updated_at`` is always refreshed on a successful write.
    """
    t = await resolve_target(session, operator.tenant_id, name)
    structlog.contextvars.bind_contextvars(audit_target_id=str(t.id))
    updates = body.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(t, k, v)
    t.updated_at = datetime.now(UTC)
    _log.info(
        "target_updated",
        target_id=str(t.id),
        name=t.name,
        tenant_id=str(operator.tenant_id),
        fields=list(updates.keys()),
    )
    return _to_full(t)
