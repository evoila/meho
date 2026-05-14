# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_ingested_operations()`` — async bulk-upsert of parsed OpenAPI ops.

G0.7-T2 (#403) of Initiative #389. The CLI verb
``meho connector ingest`` (T5) and the REST route ``POST
/api/v1/connectors/ingest`` (T6) both reduce to a single call to this
helper after the parser (T1 #401) produces the
:class:`EndpointDescriptorProto` list. Parallel pathway to
:func:`~meho_backplane.operations.typed_register.register_typed_operation`:
both write :class:`EndpointDescriptor` rows under the same natural
key, but typed registration runs at connector-init time with a Python
callable as the dispatch handle, whereas ingested registration runs
at operator-triggered ingest time with ``method`` / ``path`` as the
dispatch handles.

Algorithm
---------

Per call (one ``spec_source`` worth of operations):

1. **Validate the inputs.** ``spec_source`` non-empty; ``operations``
   non-empty list of :class:`EndpointDescriptorProto`. ``op_id``
   values within the batch must be unique — duplicates indicate a
   parser bug and we refuse to silently clobber within a single call.
2. **Auto-register the connector class** via
   :func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`.
   First call lands a :class:`HttpConnector` shim;
   :attr:`IngestionResult.connector_registered` reports whether a
   new class was added (``True`` first time, ``False`` thereafter).
3. **Look up existing rows** for the natural-key family
   ``(product, version, impl_id, tenant_id, op_id IN incoming)`` in
   one query.
4. **Pre-detect op-id collisions** across spec sources by inspecting
   each existing row's ``tags`` for a ``spec:<source>`` marker; a
   different marker than the incoming ``spec_source`` is the
   collision case. Raise :class:`OpIdCollision` with all offenders
   in one shot so the operator sees the full punch list rather than
   one error per re-run.
5. **For each proto:**

   * No existing row → INSERT a new descriptor with
     ``source_kind='ingested'``, ``is_enabled=False`` (staged), the
     parser-derived columns, the ``spec:<source>`` tag injected (if
     not already present from the parser), and a fresh embedding
     computed from ``summary + description + custom_description +
     tags``.
   * Existing row with matching embedding-text hash → skip-re-embed:
     UPDATE the non-embedding-text columns (``method`` / ``path`` /
     ``parameter_schema`` / ``response_schema`` / ``safety_level`` /
     ``requires_approval``) and the ``spec:<source>`` tag, advance
     ``updated_at``. Counts as ``skipped`` in the result. The
     skip-re-embed branch is the operationally critical path for
     repeated ingestion runs — re-ingesting vcenter.yaml's 961
     operations without spec changes avoids 961 ONNX inferences.
   * Existing row with different embedding-text hash → re-embed:
     UPDATE every body-derived column plus ``embedding``, advance
     ``updated_at``. Counts as ``updated``.

6. **Commit** (helper-owned session) or **flush** (caller-owned
   session). The caller-owned-session branch matches the
   :func:`register_typed_operation` shape so the CLI verb / REST
   route can stage ingestion inside a larger audit-emitting
   transaction.

Idempotency contract
--------------------

* Re-running with the same args produces no embedding service calls
  for unchanged rows (verified via the call-count assertion in
  tests) and leaves ``inserted_count=0``,
  ``updated_count=0``, ``skipped_count=len(operations)``.
* Re-running after a parser-side change to a single op triggers
  exactly one re-embed for that op; the rest skip.
* Re-running with a fresh ``spec_source`` against the same
  connector merges: new op-ids land as inserts; pre-existing op-ids
  whose ``spec:*`` marker came from the original spec raise
  :class:`OpIdCollision` (multi-spec merge requires disjoint
  op_ids in v0.2).

Stale operations
----------------

Ops that exist in the DB under this connector but do NOT appear in
the incoming ``operations`` list are NOT auto-deleted in v0.2.
Operators handle obsolete ops via T4's per-op
``edit_op(is_enabled=False)`` flow. Auto-deletion on re-ingest is
v0.2.next once the audit story for "operator-disabled vs
spec-removed" is settled.

Connector class auto-registration
---------------------------------

See :mod:`.connector_registration` for the design notes. The auto-shim
gets the connector resolvable + dispatchable for read-only ops with
default auth; per-product auth quirks (G3.1 vSphere session creation,
G3.5 NSX XSRF, G3.6 vCF dual-plane) ship as real subclasses per G3.x
Initiative and REPLACE this shim at code-merge time.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.embed import (
    build_embedding_text,
    compute_embedding_text_hash,
    encode_endpoint_text,
)
from meho_backplane.operations.ingest.connector_registration import (
    ensure_connector_class_registered,
)
from meho_backplane.operations.ingest.exceptions import OpIdCollision
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "IngestionResult",
    "register_ingested_operations",
]

_SPEC_TAG_PREFIX = "spec:"


class IngestionResult(BaseModel):
    """Outcome of one :func:`register_ingested_operations` call.

    Returned by the helper and surfaced verbatim by the CLI verb
    ``meho connector ingest`` (T5) and the REST route
    ``POST /api/v1/connectors/ingest`` (T6); both render the field
    set as a JSON object so operators (and the test harness in T8)
    can assert against counts without parsing prose.

    Attributes
    ----------
    inserted_count, updated_count, skipped_count:
        Per-op outcomes. ``inserted`` = new row; ``updated`` =
        existing row, embedding text changed → re-embed; ``skipped``
        = existing row, embedding text unchanged → no re-embed. The
        sum is ``len(operations)`` on every successful call.
    connector_registered:
        ``True`` when this call was the first to ingest the
        ``(product, version, impl_id)`` triple and registered a
        :class:`HttpConnector` shim into the v2 registry. ``False``
        on subsequent calls (the shim — or a real per-product
        subclass — was already present).
    operations_grouped:
        Always ``False`` from this helper. T3 (LLM grouping)
        flips groups onto rows in a follow-up pass; T2 leaves
        ``group_id`` NULL.
    """

    model_config = ConfigDict(frozen=True)

    inserted_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    connector_registered: bool
    operations_grouped: bool = False


def _normalise_tags_for_persistence(
    proto_tags: Sequence[str],
    *,
    spec_source: str,
) -> list[str]:
    """Return *proto_tags* with a single ``spec:<source>`` marker present.

    The parser (T1) may already inject the ``spec:<source>`` tag when
    callers passed ``spec_source`` through ``parse_openapi``; the
    helper must still cope with callers (notably T8's vSphere canary
    smoke harness) that parse first and pass the resulting protos
    through ``register_ingested_operations`` with a different
    ``spec_source`` argument. The function:

    * Filters out **any** existing ``spec:*`` markers from the
      incoming tags — they belong to whatever code path produced the
      proto, not to this ingestion call.
    * Appends ``spec:<source>`` at the end so the tag is
      deterministically present.

    Preserves the relative ordering of non-spec tags so the parser's
    upstream tag list stays readable in the persisted column.
    """
    cleaned = [tag for tag in proto_tags if not tag.startswith(_SPEC_TAG_PREFIX)]
    cleaned.append(f"{_SPEC_TAG_PREFIX}{spec_source}")
    return cleaned


def _extract_spec_source(tags: Sequence[str]) -> str | None:
    """Return the ``spec_source`` value carried by *tags*, or ``None``.

    Reads the first ``spec:<value>`` marker — there should only ever
    be one (Tag normalisation above strips duplicates on every
    upsert). A row without a ``spec:*`` tag pre-dates multi-spec
    merge and is treated as "no spec source recorded" in collision
    diagnostics.
    """
    for tag in tags:
        if tag.startswith(_SPEC_TAG_PREFIX):
            return tag[len(_SPEC_TAG_PREFIX) :]
    return None


def _validate_batch_unique_op_ids(operations: Sequence[EndpointDescriptorProto]) -> None:
    """Reject a batch with duplicate ``op_id`` values across protos.

    Parser-produced batches always have unique op-ids (one per
    method+path tuple); a duplicate indicates a bug upstream of T2.
    Failing fast here surfaces the parser regression at the
    ingestion boundary rather than silently letting the second
    duplicate clobber the first within the same call.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for proto in operations:
        if proto.op_id in seen:
            duplicates.add(proto.op_id)
        seen.add(proto.op_id)
    if duplicates:
        raise ValueError(
            f"register_ingested_operations: duplicate op_id(s) within batch: {sorted(duplicates)!r}"
        )


def _detect_collisions(
    existing_by_op_id: dict[str, EndpointDescriptor],
    *,
    spec_source: str,
) -> dict[str, str]:
    """Return ``{op_id: existing_spec_source}`` for any cross-spec collisions.

    Empty dict when none — the caller treats that as the "no
    collision" case and proceeds to upsert. A non-empty dict
    triggers :class:`OpIdCollision`.
    """
    collisions: dict[str, str] = {}
    for op_id, existing in existing_by_op_id.items():
        existing_source = _extract_spec_source(existing.tags)
        # Untagged legacy rows (existing_source is None) are NOT a
        # collision — they pre-date multi-spec merge and the
        # ingestion is allowed to claim them by stamping the spec
        # marker on the row.
        if existing_source is not None and existing_source != spec_source:
            collisions[op_id] = existing_source
    return collisions


async def register_ingested_operations(
    *,
    product: str,
    version: str,
    impl_id: str,
    spec_source: str,
    operations: Sequence[EndpointDescriptorProto],
    base_url: str | None = None,
    tenant_id: UUID | None = None,
    session: AsyncSession | None = None,
    embedding_service: EmbeddingService | None = None,
) -> IngestionResult:
    """Bulk-upsert parsed operations into ``endpoint_descriptor`` for one spec.

    Parameters
    ----------
    product, version, impl_id:
        Natural-key coordinates for the connector
        ``(product, version, impl_id)`` triple. The same triple is
        used by typed registrations and by registry v2 routing.
    spec_source:
        Tag value identifying which upstream document the
        ``operations`` came from (e.g. ``"vcenter.yaml"``,
        ``"vi-json.yaml"``). Materialised as a ``spec:<value>`` tag
        on every persisted row so the operator review queue (T4)
        can browse per spec, and so multi-spec collisions can be
        detected on subsequent ingestions. Must be a non-empty
        string.
    operations:
        Parser-produced :class:`EndpointDescriptorProto` list. The
        helper rejects empty batches and batches with duplicate
        op-ids (parser regression guard).
    base_url:
        Optional default base URL for the auto-registered
        :class:`HttpConnector` shim. Recorded as a class attribute
        on the dynamically-created class so a future per-product
        subclass can read it without re-plumbing ingestion. The
        :meth:`HttpConnector._base_url` default reads from the
        target's host/port — the recorded value is informational in
        v0.2.
    tenant_id:
        ``None`` for built-in / global ingestion (shipped product
        defaults); a UUID for tenant-curated ingestion. The
        per-tenant scope writes rows under the
        ``endpoint_descriptor_tenant_idx`` partial-unique index;
        built-in rows write under
        ``endpoint_descriptor_global_idx``.
    session:
        Caller-owned :class:`AsyncSession` for stage-in-larger-tx
        callers (T5 CLI / T6 REST). When ``None`` the helper opens
        its own session, commits, and closes.
    embedding_service:
        Test seam for the :class:`EmbeddingService`. Production
        callers pass ``None`` so the helper resolves the
        process-wide singleton.

    Returns
    -------
    IngestionResult
        Outcome counts plus the connector-registration flag. See
        :class:`IngestionResult` for the field semantics.

    Raises
    ------
    ValueError
        ``spec_source`` empty / whitespace, ``operations`` empty, or
        the batch contains duplicate ``op_id`` values.
    OpIdCollision
        One or more incoming op-ids collide with existing rows whose
        ``spec:*`` tag names a *different* spec source. No rows are
        written when this fires.
    """
    if not spec_source or not spec_source.strip():
        raise ValueError(
            "register_ingested_operations: spec_source must be a non-empty string "
            f"(received {spec_source!r})"
        )
    if not operations:
        raise ValueError("register_ingested_operations: operations list is empty")
    _validate_batch_unique_op_ids(operations)

    connector_registered = ensure_connector_class_registered(
        product=product,
        version=version,
        impl_id=impl_id,
        base_url=base_url,
    )

    if session is not None:
        result = await _register_in_session(
            session,
            product=product,
            version=version,
            impl_id=impl_id,
            spec_source=spec_source,
            operations=operations,
            tenant_id=tenant_id,
            embedding_service=embedding_service,
            commit=False,
        )
    else:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as owned_session:
            result = await _register_in_session(
                owned_session,
                product=product,
                version=version,
                impl_id=impl_id,
                spec_source=spec_source,
                operations=operations,
                tenant_id=tenant_id,
                embedding_service=embedding_service,
                commit=True,
            )

    return IngestionResult(
        inserted_count=result["inserted_count"],
        updated_count=result["updated_count"],
        skipped_count=result["skipped_count"],
        connector_registered=connector_registered,
        operations_grouped=False,
    )


async def _register_in_session(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    spec_source: str,
    operations: Sequence[EndpointDescriptorProto],
    tenant_id: UUID | None,
    embedding_service: EmbeddingService | None,
    commit: bool,
) -> dict[str, int]:
    """Inner upsert path — runs against *session* without owning its lifecycle.

    Split out from :func:`register_ingested_operations` so the public
    helper can branch on caller-owned-vs-helper-owned session without
    duplicating the upsert loop. Returns the count breakdown
    (inserted / updated / skipped) the outer helper folds into
    :class:`IngestionResult` together with the connector-registration
    flag.

    Tenant-aware natural-key lookup: ``tenant_id IS NULL`` for
    built-in ingestion, ``tenant_id = :uuid`` for tenant-curated.
    The two paths share the same upsert logic but route the
    descriptor INSERT to the matching partial-unique index.
    """
    log = structlog.get_logger(__name__)
    now = datetime.now(UTC)

    op_ids = [proto.op_id for proto in operations]
    # ``IN (...)`` against the natural-key fields. SQLAlchemy emits
    # a tuple expansion under PG and SQLite; for a batch of <=1000
    # op-ids (vcenter.yaml's 961 paths, vi-json.yaml's 2,195) the
    # round-trip is one query.
    query = select(EndpointDescriptor).where(
        EndpointDescriptor.product == product,
        EndpointDescriptor.version == version,
        EndpointDescriptor.impl_id == impl_id,
        EndpointDescriptor.op_id.in_(op_ids),
    )
    if tenant_id is None:
        query = query.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        query = query.where(EndpointDescriptor.tenant_id == tenant_id)
    result = await session.execute(query)
    existing_by_op_id: dict[str, EndpointDescriptor] = {row.op_id: row for row in result.scalars()}

    collisions = _detect_collisions(existing_by_op_id, spec_source=spec_source)
    if collisions:
        raise OpIdCollision(
            incoming_spec_source=spec_source,
            colliding_op_ids=list(collisions),
            existing_spec_sources=collisions,
        )

    inserted_count = 0
    updated_count = 0
    skipped_count = 0

    for proto in operations:
        tags = _normalise_tags_for_persistence(proto.tags, spec_source=spec_source)
        incoming_text = build_embedding_text(
            summary=proto.summary or "",
            description=proto.description or "",
            custom_description=None,
            tags=tags,
        )
        incoming_hash = compute_embedding_text_hash(incoming_text)

        existing = existing_by_op_id.get(proto.op_id)
        if existing is not None:
            existing_text = build_embedding_text(
                summary=existing.summary or "",
                description=existing.description or "",
                custom_description=existing.custom_description,
                tags=existing.tags,
            )
            existing_hash = compute_embedding_text_hash(existing_text)

            if existing_hash == incoming_hash:
                # Skip-re-embed: parser output and persisted text
                # match. Update the non-text columns (parameter
                # schema, response schema, method, path, etc.) when
                # they differ; advance ``updated_at``.
                existing.method = proto.method
                existing.path = proto.path
                existing.parameter_schema = dict(proto.parameter_schema)
                existing.response_schema = (
                    dict(proto.response_schema) if proto.response_schema is not None else None
                )
                existing.safety_level = proto.safety_level
                existing.requires_approval = proto.requires_approval
                existing.tags = tags
                existing.updated_at = now
                skipped_count += 1
                continue

            # Re-embed: persisted text differs from parser output.
            embedding = await encode_endpoint_text(
                incoming_text,
                service=embedding_service,
            )
            existing.method = proto.method
            existing.path = proto.path
            existing.summary = proto.summary
            existing.description = proto.description
            existing.parameter_schema = dict(proto.parameter_schema)
            existing.response_schema = (
                dict(proto.response_schema) if proto.response_schema is not None else None
            )
            existing.safety_level = proto.safety_level
            existing.requires_approval = proto.requires_approval
            existing.tags = tags
            existing.embedding = embedding
            existing.updated_at = now
            updated_count += 1
            continue

        # First-register path: brand-new row in staged state.
        embedding = await encode_endpoint_text(
            incoming_text,
            service=embedding_service,
        )
        descriptor = EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=proto.op_id,
            source_kind="ingested",
            method=proto.method,
            path=proto.path,
            # ``handler_ref`` is the typed-op dispatch handle; ingested
            # ops resolve their dispatch target via ``method`` + ``path``
            # against the registered HttpConnector subclass.
            handler_ref=None,
            summary=proto.summary,
            description=proto.description,
            # Group assignment is T3's responsibility; T2 leaves NULL.
            group_id=None,
            tags=tags,
            parameter_schema=dict(proto.parameter_schema),
            response_schema=(
                dict(proto.response_schema) if proto.response_schema is not None else None
            ),
            llm_instructions=None,
            safety_level=proto.safety_level,
            requires_approval=proto.requires_approval,
            # Staged state: agent's search_operations (T8) must not
            # surface this row until the operator review queue (T4)
            # transitions it to enabled.
            is_enabled=False,
            embedding=embedding,
            custom_description=None,
            custom_notes=None,
            created_at=now,
            updated_at=now,
        )
        session.add(descriptor)
        inserted_count += 1

    await session.flush()
    if commit:
        await session.commit()

    log.info(
        "ingested_operations_registered",
        product=product,
        version=version,
        impl_id=impl_id,
        spec_source=spec_source,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        inserted=inserted_count,
        updated=updated_count,
        skipped=skipped_count,
    )

    return {
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
    }
