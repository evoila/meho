# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-op upsert helpers for the spec-ingestion bulk-upsert pipeline.

Private support module for
:func:`~meho_backplane.operations.ingest.register_ingested.register_ingested_operations`
(G0.7-T2 #403). Split out so the public module stays focused on the
batch-level orchestration (collision detection, connector class
auto-registration, session ownership) while the per-row branches
live here.

The three persistence branches -- skip-re-embed, re-embed, and
first-register -- each have a dedicated helper. Their orchestrator
:func:`_upsert_one_operation` is what
:func:`register_ingested_operations` calls per row in the batch.

Nothing here is part of the public ``meho_backplane.operations.ingest``
surface; the underscore prefix is the contract. v0.2.next refactors
are free to reshape the helpers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.embed import (
    build_embedding_text,
    compute_embedding_text_hash,
    encode_endpoint_text,
)
from meho_backplane.operations.ingest.exceptions import OpIdCollision
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.retrieval.embedding import EmbeddingService

_SPEC_TAG_PREFIX = "spec:"

__all__ = [
    "UpsertContext",
    "build_upsert_context",
    "upsert_one_operation",
]


@dataclass(frozen=True, slots=True)
class UpsertContext:
    """Per-op derived state shared across the upsert branches.

    Bundling the lookup-key components + the precomposed embedding
    text/hash + the spec-tagged ``tags`` list into one frozen dataclass
    keeps each branch helper's signature small (one positional context
    arg) without losing the natural-key coordinates each branch needs
    for its logging / row writes.
    """

    tenant_id: UUID | None
    product: str
    version: str
    impl_id: str
    spec_source: str
    proto: EndpointDescriptorProto
    tags_with_marker: list[str]
    incoming_text: str
    incoming_hash: str
    now: datetime


def build_upsert_context(
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
    spec_source: str,
    proto: EndpointDescriptorProto,
    now: datetime,
) -> UpsertContext:
    """Compose the per-op upsert context from caller args + parser proto.

    The persisted row's ``tags`` value is ``proto.tags`` with the
    synthetic ``f"spec:{spec_source}"`` marker appended -- the helper
    accepts the bare label (``"vcenter.yaml"``) and formats the prefix
    itself per the Task #403 API contract.
    """
    tags_with_marker = [*proto.tags, f"spec:{spec_source}"]
    incoming_text = build_embedding_text(
        summary=proto.summary or "",
        description=proto.description or "",
        custom_description=None,
        tags=tags_with_marker,
    )
    return UpsertContext(
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        spec_source=spec_source,
        proto=proto,
        tags_with_marker=tags_with_marker,
        incoming_text=incoming_text,
        incoming_hash=compute_embedding_text_hash(incoming_text),
        now=now,
    )


def _normalize_spec_source(spec_source: str) -> str:
    """Reduce a ``spec_source`` label to its prefix-free logical identity.

    Cross-call dedup keys on the *logical* spec a row came from, not on
    the exact rendering of its audit label. The catalog shipped-spec
    on-ramp labels its source ``spec:<resource>`` (#1975), so the same
    logical spec can reach the comparison carrying a different number of
    leading ``spec:`` prefixes on each side -- e.g. a persisted marker
    that round-trips ``spec:<resource>`` versus an incoming
    ``spec:<resource>`` re-submission. Stripping the leading ``spec:``
    layers off both sides makes those compare equal (idempotent
    re-ingest) while leaving genuinely distinct sources apart:
    ``file:///a.yaml`` never normalizes onto ``spec:b.yaml``.
    """
    while spec_source.startswith(_SPEC_TAG_PREFIX):
        spec_source = spec_source[len(_SPEC_TAG_PREFIX) :]
    return spec_source


def _extract_persisted_spec_source(existing: EndpointDescriptor) -> str | None:
    """Recover the ``spec_source`` of an already-persisted row.

    ``build_upsert_context`` appends the synthetic ``f"spec:{spec_source}"``
    marker as the row's **last** tag, so the marker is the authoritative
    record of the source. The parser also persists the raw ``spec_source``
    verbatim as a tag (:func:`parse_openapi`); for a catalog shipped-spec
    source -- already ``spec:<resource>`` -- that verbatim tag is itself
    ``spec:``-prefixed and sits *before* the marker. Scanning from the
    end returns the marker instead of letting the verbatim tag shadow it:
    a first-match scan recovered the verbatim tag's payload, one ``spec:``
    layer short of the real source, so every catalog re-ingest then
    collided with its own rows (#2274). If no ``spec:`` tag is present
    (older rows, hand-edited fixtures) return ``None`` and let the caller
    skip the cross-call check rather than raise a spurious collision.
    """
    for tag in reversed(existing.tags or []):
        if tag.startswith(_SPEC_TAG_PREFIX):
            return tag[len(_SPEC_TAG_PREFIX) :]
    return None


def _check_cross_source_collision(existing: EndpointDescriptor, ctx: UpsertContext) -> None:
    """Raise :exc:`OpIdCollision` when a persisted row came from a *different* spec.

    A row already exists under this ``(product, version, impl_id, op_id)``
    natural key. If a prior ingest wrote it under a different
    ``spec_source``, silently re-embedding would overwrite that spec's
    method / path / summary / schemas with this call's payload -- never
    what the operator wants. Per Task #403 they must instead see a
    structured exception naming both sources so they can rename one
    ``op_id`` or skip the offending spec.

    The equality keys on the *logical* spec identity, not the rendered
    audit label. The catalog shipped-spec on-ramp labels its source
    ``spec:<resource>`` (#1975), so the same spec can reach this guard
    carrying a different number of leading ``spec:`` prefixes on each
    side; :func:`_normalize_spec_source` collapses them so a same-spec
    re-ingest stays a no-op instead of colliding with its own rows
    (#2274). A missing marker (older rows, hand-edited fixtures) skips
    the check. The raised exception carries the *raw* labels for
    diagnostics.
    """
    existing_spec_source = _extract_persisted_spec_source(existing)
    if existing_spec_source is None:
        return
    if _normalize_spec_source(existing_spec_source) == _normalize_spec_source(ctx.spec_source):
        return
    raise OpIdCollision(
        op_ids=[ctx.proto.op_id],
        product=ctx.product,
        version=ctx.version,
        impl_id=ctx.impl_id,
        existing_spec_source=existing_spec_source,
        incoming_spec_source=ctx.spec_source,
    )


async def _lookup_existing_descriptor(
    session: AsyncSession,
    ctx: UpsertContext,
) -> EndpointDescriptor | None:
    """Find an existing row matching the natural key + tenant partial index."""
    stmt = select(EndpointDescriptor).where(
        EndpointDescriptor.product == ctx.product,
        EndpointDescriptor.version == ctx.version,
        EndpointDescriptor.impl_id == ctx.impl_id,
        EndpointDescriptor.op_id == ctx.proto.op_id,
    )
    if ctx.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == ctx.tenant_id)
    return (await session.execute(stmt)).scalar_one_or_none()


def _apply_skip_reembed(existing: EndpointDescriptor, ctx: UpsertContext) -> None:
    """Update non-embedding fields on a body-hash-matched row.

    Leave ``summary`` / ``description`` / ``tags`` alone (the hash
    match proved they're equal) so the ORM identity map stays
    consistent; only refresh the proto-derived non-embedding fields.
    """
    existing.method = ctx.proto.method
    existing.path = ctx.proto.path
    existing.parameter_schema = ctx.proto.parameter_schema
    existing.response_schema = ctx.proto.response_schema
    existing.safety_level = ctx.proto.safety_level
    existing.requires_approval = ctx.proto.requires_approval
    existing.updated_at = ctx.now


def _apply_reembed_update(
    existing: EndpointDescriptor,
    ctx: UpsertContext,
    embedding: list[float],
) -> None:
    """Re-embed path: existing row, embedding text changed."""
    existing.method = ctx.proto.method
    existing.path = ctx.proto.path
    existing.summary = ctx.proto.summary
    existing.description = ctx.proto.description
    existing.tags = ctx.tags_with_marker
    existing.parameter_schema = ctx.proto.parameter_schema
    existing.response_schema = ctx.proto.response_schema
    existing.safety_level = ctx.proto.safety_level
    existing.requires_approval = ctx.proto.requires_approval
    existing.embedding = embedding
    existing.updated_at = ctx.now


def _build_new_descriptor(
    ctx: UpsertContext,
    embedding: list[float],
) -> EndpointDescriptor:
    """First-register path: brand-new row populated from the proto."""
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        product=ctx.product,
        version=ctx.version,
        impl_id=ctx.impl_id,
        op_id=ctx.proto.op_id,
        source_kind="ingested",
        method=ctx.proto.method,
        path=ctx.proto.path,
        handler_ref=None,
        summary=ctx.proto.summary,
        description=ctx.proto.description,
        group_id=None,
        tags=ctx.tags_with_marker,
        parameter_schema=ctx.proto.parameter_schema,
        response_schema=ctx.proto.response_schema,
        llm_instructions=None,
        safety_level=ctx.proto.safety_level,
        requires_approval=ctx.proto.requires_approval,
        is_enabled=False,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=ctx.now,
        updated_at=ctx.now,
    )


async def upsert_one_operation(
    session: AsyncSession,
    ctx: UpsertContext,
    *,
    embedding_service: EmbeddingService | None,
) -> str:
    """Upsert one :class:`EndpointDescriptor` row from a parser proto.

    Returns one of ``"inserted"`` / ``"updated"`` / ``"skipped"`` so
    the caller can tally the counts for :class:`IngestionResult`. The
    three branches (skip-re-embed / re-embed / first-register) live
    in dedicated helpers; this function is the orchestrator that
    chooses between them.
    """
    existing = await _lookup_existing_descriptor(session, ctx)

    if existing is not None:
        # Same-spec re-ingest falls through to the skip-re-embed /
        # re-embed branches below; only a genuine cross-source op_id
        # clash raises (see the helper for the normalization rationale).
        _check_cross_source_collision(existing, ctx)

        existing_text = build_embedding_text(
            summary=existing.summary or "",
            description=existing.description or "",
            custom_description=existing.custom_description,
            tags=existing.tags,
        )
        if compute_embedding_text_hash(existing_text) == ctx.incoming_hash:
            _apply_skip_reembed(existing, ctx)
            await session.flush()
            return "skipped"

        embedding = await encode_endpoint_text(ctx.incoming_text, service=embedding_service)
        _apply_reembed_update(existing, ctx, embedding)
        await session.flush()
        return "updated"

    embedding = await encode_endpoint_text(ctx.incoming_text, service=embedding_service)
    descriptor = _build_new_descriptor(ctx, embedding)
    session.add(descriptor)
    await session.flush()
    return "inserted"
