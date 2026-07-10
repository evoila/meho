# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Persist + read durable spec-ingest provenance (#2291).

One :class:`~meho_backplane.db.models.SpecProvenance` row per accepted
spec ingest, keyed on the connector triple + audit ``uri`` within a
tenant scope. The register phase
(:meth:`~meho_backplane.operations.ingest.pipeline.IngestionPipeline._run_register_phase`)
writes a row per spec after its descriptor rows land; the review
service reads them back to surface provenance on
:class:`~meho_backplane.operations.ingest.payload.ConnectorReviewPayload`.

Provenance is deliberately its own transaction, independent of the
descriptor upsert: it is record-and-surface metadata, not a
dispatch-time invariant, so a provenance write must never roll back the
operations it describes (and vice versa). Re-ingesting the same spec
updates the existing row in place (new ``sha256`` + ``ingested_at``)
rather than accumulating duplicates — the ``(scope, uri)`` natural key
is the dedup axis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.db.models import SpecProvenance

__all__ = [
    "load_spec_provenance",
    "upsert_spec_provenance",
]


async def upsert_spec_provenance(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
    uri: str,
    sha256: str,
    origin: str,
    operator_sub: str | None,
) -> None:
    """Insert or update the provenance row for one accepted spec ingest.

    Keyed on ``(tenant_id, product, version, impl_id, uri)`` — the same
    scope discipline the descriptor upsert uses (``tenant_id IS NULL``
    for built-in/global rows). A first ingest inserts; a re-ingest of the
    same spec under the same key updates ``sha256`` / ``origin`` /
    ``operator_sub`` and refreshes ``ingested_at`` so the row always
    reflects the latest accepted ingest. Different content under the same
    ``uri`` changes the stored ``sha256`` in place.

    Owns its own session + commit (see the module docstring): a
    provenance write is decoupled from the descriptor transaction.
    """
    async with sessionmaker() as session:
        existing = await _lookup_provenance(
            session,
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            uri=uri,
        )
        if existing is None:
            session.add(
                SpecProvenance(
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    uri=uri,
                    sha256=sha256,
                    origin=origin,
                    operator_sub=operator_sub,
                    ingested_at=datetime.now(UTC),
                )
            )
        else:
            existing.sha256 = sha256
            existing.origin = origin
            existing.operator_sub = operator_sub
            existing.ingested_at = datetime.now(UTC)
        await session.commit()


async def load_spec_provenance(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
) -> list[SpecProvenance]:
    """Return every provenance row for a connector scope, ordered by ``uri``.

    Scoped exactly like the resolved review scope: ``tenant_id IS NULL``
    reads the built-in/global rows, a tenant UUID reads that tenant's
    rows. Deterministic ``uri`` ordering keeps the review payload stable
    across calls. Runs on the caller-owned session so it shares the
    review render's transaction snapshot.
    """
    stmt = select(SpecProvenance).where(
        SpecProvenance.product == product,
        SpecProvenance.version == version,
        SpecProvenance.impl_id == impl_id,
    )
    if tenant_id is None:
        stmt = stmt.where(SpecProvenance.tenant_id.is_(None))
    else:
        stmt = stmt.where(SpecProvenance.tenant_id == tenant_id)
    stmt = stmt.order_by(SpecProvenance.uri)
    return list((await session.execute(stmt)).scalars().all())


async def _lookup_provenance(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
    uri: str,
) -> SpecProvenance | None:
    """Find the provenance row matching the natural key within its scope."""
    stmt = select(SpecProvenance).where(
        SpecProvenance.product == product,
        SpecProvenance.version == version,
        SpecProvenance.impl_id == impl_id,
        SpecProvenance.uri == uri,
    )
    if tenant_id is None:
        stmt = stmt.where(SpecProvenance.tenant_id.is_(None))
    else:
        stmt = stmt.where(SpecProvenance.tenant_id == tenant_id)
    return (await session.execute(stmt)).scalar_one_or_none()
