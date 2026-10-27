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
  shipped catalog is stamped) and again when a call finds no connector
  class, so a replica that was already running when another replica took
  the ingest catches up without a restart.

Restoring goes through the same
:meth:`~meho_backplane.operations.ingest.service.ReviewService.record_profile_stamp`
step the ingest uses, so it registers a class and never enables an
operation, lowers a risk level, or removes an approval. A connector key
that already has a class (a hand-written connector, a shipped profile, or
an earlier restore) is left alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.profile import NAMED_AUTH_SCHEMES, ExecutionProfile
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import IngestedAuthProfile
from meho_backplane.operations.ingest._llm_grouping_internals import build_connector_id
from meho_backplane.operations.ingest.connector_registration import synthesise_profiled_class
from meho_backplane.operations.ingest.ingest_profile import build_ingest_execution_profile
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


async def _restore_row(row: IngestedAuthProfile) -> bool:
    """Build and stamp the connector class for one saved row.

    Returns ``True`` when a new class was registered. A row that no longer
    builds a valid profile (a scheme removed from the closed list, bad
    field names) is logged and skipped, so one bad row cannot stop the
    backplane from starting.
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
    registered = await ReviewService(_restore_operator(row.tenant_id)).record_profile_stamp(
        connector_id, tenant_id=row.tenant_id, connector_class=connector_class
    )
    if registered:
        _log.info(
            "ingested_profile_restored",
            connector_id=connector_id,
            auth_scheme=row.auth_scheme,
            connector_class=connector_class.__name__,
        )
    return registered


async def restore_ingested_profiled_connectors(
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Register a profiled connector class for every saved login type.

    Skips rows whose ``(product, version, impl_id)`` already has a class,
    so it is safe to run twice. Rows are read oldest first, so when two
    tenants saved the same key the older choice wins, the same as the
    in-memory table before a restart. Returns how many classes it
    registered.
    """
    maker = sessionmaker if sessionmaker is not None else get_sessionmaker()
    async with maker() as session:
        stmt = select(IngestedAuthProfile).order_by(
            IngestedAuthProfile.updated_at, IngestedAuthProfile.id
        )
        rows = list((await session.execute(stmt)).scalars().all())

    restored = 0
    for row in rows:
        if (row.product, row.version, row.impl_id) in all_connectors_v2():
            continue
        if await _restore_row(row):
            restored += 1
    return restored
