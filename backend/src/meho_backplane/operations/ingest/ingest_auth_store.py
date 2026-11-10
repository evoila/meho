# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save and restore the login type of an ingested connector (#5016).

``meho connector ingest --auth-scheme <name>`` (#2289) builds a
:class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` from the
chosen login type and registers it in the connector table. That table lives
in memory, so before this module a restart dropped the class: the
connector's operations stayed enabled, but every call failed with
``no_connector``.

This module saves the choice in the ``ingested_auth_profile`` table
(:class:`~meho_backplane.db.models.IngestedAuthProfile`) and builds the
class again from it:

* :func:`save_ingest_auth_profile` runs on every real ingest with a login
  type. It stores the scheme name and the secret field **names** only, the
  same closed selection the ingest request accepts. No secret values, no
  free-form login config.
* :func:`restore_ingested_profiled_connectors` runs at start (after the
  shipped catalog is stamped) and again, scoped to the target's product,
  before every call to an ingested operation resolves its connector. So a
  replica that was already running when another replica took the ingest
  catches up without a restart, also when it already holds another
  connector for the same product and the target prefers the new one.

Restoring goes through the same
:meth:`~meho_backplane.operations.ingest.service.ReviewService.record_login_setup`
step the ingest uses, so it registers a class and never enables an
operation, lowers a risk level, or removes an approval. Each row goes into
the table of its own scope: a tenant's row applies only to that tenant's
calls. A scope that already holds the same login type is left alone. A
scope that holds another login type (the row was replaced on another
replica) gets the saved one, with an audit row. A key served by a
hand-written connector or a shipped login setup keeps that class.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import shim_kind
from meho_backplane.connectors.profile import NAMED_AUTH_SCHEMES, ExecutionProfile
from meho_backplane.connectors.registry import get_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import IngestedAuthProfile
from meho_backplane.operations.ingest._llm_grouping_internals import build_connector_id
from meho_backplane.operations.ingest.connector_registration import synthesise_profiled_class
from meho_backplane.operations.ingest.ingest_profile import build_ingest_execution_profile
from meho_backplane.operations.ingest.login_setup import LOGIN_SETUP_APPLIED, current_login_setup
from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "RESTORE_OPERATOR_SUB",
    "restore_ingested_profiled_connectors",
    "save_ingest_auth_profile",
]

_log = structlog.get_logger(__name__)

#: ``operator_sub`` on the stamp audit rows a restore writes. A restore is a
#: system action, so it uses a stable ``system:*`` sub, like the boot-time
#: catalog stamp.
RESTORE_OPERATOR_SUB = "system:ingested-profile-restore"


def _scope_filter(tenant_id: UUID | None) -> ColumnElement[bool]:
    if tenant_id is None:
        return IngestedAuthProfile.tenant_id.is_(None)
    return IngestedAuthProfile.tenant_id == tenant_id


async def save_ingest_auth_profile(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
    profile: ExecutionProfile,
    operator_sub: str,
) -> None:
    """Insert or replace the saved login type for one connector scope.

    Keyed on ``(tenant_id, product, version, impl_id)``. A re-ingest with a
    login type replaces the scheme and field names, so the row always holds
    the latest choice. Commits in its own session.
    """
    async with sessionmaker() as session:
        stmt = select(IngestedAuthProfile).where(
            IngestedAuthProfile.product == product,
            IngestedAuthProfile.version == version,
            IngestedAuthProfile.impl_id == impl_id,
            _scope_filter(tenant_id),
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = IngestedAuthProfile(
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            )
            session.add(row)
        row.auth_scheme = profile.auth.scheme
        row.secret_fields = list(profile.auth.secret_fields)
        row.operator_sub = operator_sub
        row.updated_at = datetime.now(UTC)
        await session.commit()


def _restore_operator(tenant_id: UUID | None) -> Operator:
    """Build the system operator that may stamp the row's scope.

    ``record_profile_stamp`` needs ``tenant_admin`` for the built-in scope
    and the row's own tenant for a tenant scope. ``raw_jwt`` is empty: a
    stamp reads no vendor credential.
    """
    return Operator(
        sub=RESTORE_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id if tenant_id is not None else uuid.UUID(int=0),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _nothing_to_restore(row: IngestedAuthProfile) -> bool:
    """Return whether the row would change nothing in memory.

    True when the row's scope already holds the row's login type, or when
    a class that serves every tenant holds the key (hand-written code, or
    a built-in login setup for a tenant's row). A cheap check before
    building anything, so the read before every call costs little.
    """
    shared = get_connector_v2(row.product, row.version, row.impl_id)
    if shared is not None:
        kind = shim_kind(shared)
        if kind == "none" or (kind == "profiled" and row.tenant_id is not None):
            return True
    current = current_login_setup(row.tenant_id, row.product, row.version, row.impl_id)
    profile = getattr(current, "profile", None)
    if profile is None:
        return False
    return profile.auth.scheme == row.auth_scheme and list(profile.auth.secret_fields) == list(
        row.secret_fields
    )


async def _restore_row(row: IngestedAuthProfile) -> bool:
    """Build and stamp the connector class for one saved row.

    Returns ``True`` when the row's scope now uses a new class (registered
    or replaced). A row that does not build a valid profile (a scheme
    outside the closed list, bad field names) is logged and skipped, so
    one bad row cannot stop the backplane from starting.
    """
    connector_id = build_connector_id(row.product, row.version, row.impl_id)
    if row.auth_scheme not in NAMED_AUTH_SCHEMES:
        _log.warning(
            "ingested_profile_restore_skipped",
            connector_id=connector_id,
            reason="unknown_auth_scheme",
            auth_scheme=row.auth_scheme,
        )
        return False
    try:
        profile = build_ingest_execution_profile(
            product=row.product,
            version=row.version,
            auth_scheme=row.auth_scheme,  # type: ignore[arg-type]
            secret_fields=tuple(row.secret_fields),
        )
    except ValidationError:
        _log.warning(
            "ingested_profile_restore_skipped",
            connector_id=connector_id,
            reason="invalid_profile",
        )
        return False
    connector_class = synthesise_profiled_class(
        product=row.product, version=row.version, impl_id=row.impl_id, profile=profile
    )
    outcome = await ReviewService(_restore_operator(row.tenant_id)).record_login_setup(
        connector_id, tenant_id=row.tenant_id, connector_class=connector_class
    )
    if outcome in LOGIN_SETUP_APPLIED:
        _log.info(
            "ingested_profile_restored",
            connector_id=connector_id,
            tenant_scope=str(row.tenant_id) if row.tenant_id is not None else None,
            auth_scheme=row.auth_scheme,
            connector_class=connector_class.__name__,
            outcome=outcome,
        )
    return outcome in LOGIN_SETUP_APPLIED


async def restore_ingested_profiled_connectors(
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    *,
    product: str | None = None,
    tenant_id: UUID | None = None,
) -> int:
    """Register a profiled connector class for every saved login type.

    Each row goes into the table of its own scope. A scope that already
    holds the row's login type is skipped, so it is safe to run twice.
    With *product* set, only the rows of that product are read; with
    *tenant_id* set, only the built-in rows and that tenant's rows. The
    dispatcher sets both before a call: the resolver looks only at
    connectors of the target's product, as the target's tenant sees them.
    Returns how many scopes now use a new class.
    """
    maker = sessionmaker if sessionmaker is not None else get_sessionmaker()
    async with maker() as session:
        stmt = select(IngestedAuthProfile).order_by(
            IngestedAuthProfile.updated_at, IngestedAuthProfile.id
        )
        if product is not None:
            stmt = stmt.where(IngestedAuthProfile.product == product)
        if tenant_id is not None:
            stmt = stmt.where(
                or_(
                    IngestedAuthProfile.tenant_id.is_(None),
                    IngestedAuthProfile.tenant_id == tenant_id,
                )
            )
        rows = list((await session.execute(stmt)).scalars().all())

    restored = 0
    for row in rows:
        if _nothing_to_restore(row):
            continue
        if await _restore_row(row):
            restored += 1
    return restored
