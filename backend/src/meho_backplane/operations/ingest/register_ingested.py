# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_ingested_operations()`` -- bulk upsert helper for parsed specs.

G0.7-T2 (#403) of Initiative #389. Takes the
:class:`~meho_backplane.operations.ingest.schemas.EndpointDescriptorProto`
output of :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`
(T1 #401) and persists it as
:class:`~meho_backplane.db.models.EndpointDescriptor` rows with
``source_kind='ingested'``.

The helper is the spec-ingested twin of
:func:`~meho_backplane.operations.typed_register.register_typed_operation`
(G0.6-T4 #395). Both write to the same ``endpoint_descriptor`` table
through the same body-hash skip-re-embed path; what differs is:

* ``source_kind='ingested'`` vs ``source_kind='typed'``.
* ``handler_ref`` is NULL on ingested rows (the dispatcher uses
  ``method`` + ``path`` for HTTP calls).
* Ingested rows land ``is_enabled=False`` so the agent's
  ``search_operations`` meta-tool (G0.6-T8 #399) hides them until the
  operator vets the connector via T4's review state machine. Typed
  rows land ``is_enabled=True`` because typed-connector authors vouch
  for them at code-review time.
* Auto-registered groups land ``review_status='staged'`` (T4 flips to
  ``'enabled'`` after operator review). Typed groups land
  ``review_status='enabled'``.
* The helper auto-registers a :class:`GenericRestConnector` shim
  against the v2 registry (G0.6-T2 #393) on the first ingest of a
  ``(product, version, impl_id)`` triple. Typed connectors register
  their own subclass at module-import time.

Multi-spec merge
----------------

vSphere ingests ``vcenter.yaml`` + ``vi-json.yaml`` under one
``connector_id='vmware-rest-9.0'``. The CLI / API caller invokes
:func:`register_ingested_operations` twice -- once per spec -- with
distinct ``spec_source`` arguments. Each call tags every row's
``tags`` with ``f"spec:{spec_source}"`` so the operator-facing
review payload distinguishes "this op came from vcenter.yaml" vs
"this op came from vi-json.yaml". The natural key
``(product, version, impl_id, op_id)`` is unique by partial index,
so two specs that happen to expose the same ``op_id`` would
silently UPDATE the first row with the second's payload -- the
helper rejects this with :exc:`OpIdCollision` before any DB write.

Idempotency
-----------

Re-running with identical args is a true no-op for the embedding
pipeline. The body-hash skip path inherits from
:mod:`meho_backplane.operations.typed_register`: the helper
recomputes the embedding text + SHA-256 hash from the persisted
row's ``summary`` / ``description`` / ``custom_description`` /
``tags``; if it matches the incoming hash, the embedding is reused
verbatim. The expensive ONNX inference is skipped on every
unchanged row.

Re-ingesting with changed operations triggers a re-embed for
changed rows only. Obsolete operations (in DB but absent from the
new ingestion) are NOT auto-deleted in v0.2 -- the operator uses
T4's disable verb to hide them. Auto-deletion lands in v0.2.next
once the review-queue ergonomics around "this op disappeared from
the upstream spec" are calibrated.

Behavioural contract
--------------------

* **First call** for a given ``(product, version, impl_id)`` triple
  -- auto-registers a :class:`GenericRestConnector` shim against
  the v2 registry, creates an :class:`OperationGroup` row per
  distinct group_key referenced (``review_status='staged'``),
  inserts every operation as a new
  :class:`EndpointDescriptor` row with ``source_kind='ingested'``,
  ``is_enabled=False``, embedding computed once per row.
* **Subsequent call** with a different ``spec_source`` against the
  same connector_id -- the shim is left in place, new operations
  are inserted under the existing connector, and rows tagged
  ``"spec:<new-source>"`` so the operator can distinguish them
  during review.
* **Re-call with unchanged operations** -- one row per op in the
  table; embedding service called zero times across the re-call
  (skip-re-embed path).
* **Re-call with changed operations** -- changed rows re-embed,
  unchanged rows skip-re-embed.

The helper opens its own transaction (or uses a caller-supplied
:class:`AsyncSession`) and commits row-by-row; an exception
mid-batch leaves the previous rows committed. This matches the
typed-register precedent and the v0.2 "ingest is operator-driven
and idempotent on retry" discipline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._lookup import dispatch_product
from meho_backplane.operations.ingest._upsert import (
    build_upsert_context,
    upsert_one_operation,
)
from meho_backplane.operations.ingest.connector_registration import (
    check_version_covered_by_registered_class,
    ensure_connector_class_registered,
)
from meho_backplane.operations.ingest.exceptions import OpIdCollision
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "IngestionResult",
    "register_ingested_operations",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Counts + flags from one :func:`register_ingested_operations` call.

    Attributes
    ----------
    inserted_count:
        Number of brand-new :class:`EndpointDescriptor` rows
        inserted -- the natural key ``(product, version, impl_id,
        op_id)`` did not previously exist.
    updated_count:
        Number of rows whose embedding text changed and so were
        re-embedded + UPDATEd.
    skipped_count:
        Number of rows whose embedding text was unchanged -- the
        body-hash skip path. ``updated_at`` is still advanced on
        these rows (so operators can grep "when did this op last
        get reregistered") but the embedding compute is skipped.
    connector_registered:
        ``True`` when the helper auto-registered a new
        :class:`GenericRestConnector` shim against the v2 registry
        for the ``(product, version, impl_id)`` triple. ``False``
        when an entry already existed (subsequent ingests of
        additional specs under the same connector_id).
    operations_grouped:
        Always ``False`` in v0.2 -- the T3 LLM-grouping pass runs
        as a separate step after T2. Present on the result type so
        T3 + T5 can flip it without a schema change.
    """

    inserted_count: int
    updated_count: int
    skipped_count: int
    connector_registered: bool
    operations_grouped: bool


def _detect_op_id_collisions(
    operations: Sequence[EndpointDescriptorProto],
    *,
    product: str,
    version: str,
    impl_id: str,
) -> None:
    """Raise :exc:`OpIdCollision` when two ops in the batch share an ``op_id``.

    Runs before any DB write so a colliding pair is caught at the
    Python boundary rather than after half the batch has been
    persisted. The partial unique index on the natural key would
    eventually catch the collision at flush time, but as an
    :class:`IntegrityError` whose message names the index, not the
    colliding values; the operator-facing CLI / API needs the
    structured exception shape to render a usable error.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for op in operations:
        if op.op_id in seen:
            duplicates.add(op.op_id)
        else:
            seen.add(op.op_id)
    if duplicates:
        raise OpIdCollision(
            op_ids=list(duplicates),
            product=product,
            version=version,
            impl_id=impl_id,
        )


# Per-op upsert helpers live in
# :mod:`meho_backplane.operations.ingest._upsert`; this module owns
# the batch-level orchestration only.


@dataclass(frozen=True, slots=True)
class _BatchCoordinates:
    """Connector coordinates + spec source threaded through the batch loop."""

    tenant_id: UUID | None
    product: str
    version: str
    impl_id: str
    spec_source: str


async def _run_upsert_loop(
    *,
    session: AsyncSession | None,
    coords: _BatchCoordinates,
    operations: Sequence[EndpointDescriptorProto],
    embedding_service: EmbeddingService | None,
) -> dict[str, int]:
    """Dispatch to :func:`_register_in_session` with the right session ownership.

    When *session* is provided, run the loop against it and defer
    commit decisions to the caller. When *session* is ``None``, open
    a helper-owned session via :func:`get_sessionmaker`, run the
    loop with per-op commits, and close on context exit.
    """
    if session is not None:
        return await _register_in_session(
            session,
            coords=coords,
            operations=operations,
            embedding_service=embedding_service,
            commit=False,
        )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as owned_session:
        return await _register_in_session(
            owned_session,
            coords=coords,
            operations=operations,
            embedding_service=embedding_service,
            commit=True,
        )


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
    """Bulk-upsert parsed operations into ``endpoint_descriptor``.

    See the module docstring for the full behavioural contract
    (multi-spec merge, idempotency invariant, connector auto-shim
    semantics, transaction-boundary discipline).

    Persisted rows carry the *dispatch-canonical* product (see
    :func:`_reconciled_row_product`), not necessarily the supplied
    ``product`` — the VCF-family long↔short reconciliation. The
    pre-flight + auto-shim registration stay on the supplied product.

    Args:
        product, version, impl_id: Connector triple. The persisted row's
            natural key is ``(reconciled_product, version, impl_id,
            op_id)`` with ``tenant_id IS NULL`` for built-in rows.
        spec_source: Logical-source tag (e.g. ``"vcenter.yaml"``)
            appended as ``f"spec:{spec_source}"`` to every row's
            ``tags`` so multi-spec ingests stay distinguishable.
        operations: Parser output from
            :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`.
        base_url: Optional default base URL for the auto-registered
            :class:`GenericRestConnector` shim.
        tenant_id: ``None`` (default) → built-in / global rows.
        session: Optional caller-owned :class:`AsyncSession` (defers
            commit to the caller). ``None`` opens a helper-owned
            session that commits row-by-row.
        embedding_service: Test seam; production callers leave
            ``None`` to resolve the process-wide singleton.

    Returns:
        :class:`IngestionResult` with per-action counts +
        ``connector_registered`` flag (was a new shim registered?) +
        ``operations_grouped`` flag (always ``False`` in v0.2; T3
        runs grouping next).

    Raises:
        OpIdCollision: Either (a) two operations in *operations* share an
            ``op_id`` (raised before any DB write by the within-batch
            scan), or (b) a prior call under the same triple persisted
            this ``op_id`` from a different ``spec_source`` (raised
            per-row in :func:`_upsert.upsert_one_operation`); the
            exception names both colliding specs.
    """
    _detect_op_id_collisions(
        operations,
        product=product,
        version=version,
        impl_id=impl_id,
    )
    connector_registered = _preflight_and_register_class(
        product=product,
        version=version,
        impl_id=impl_id,
        base_url=base_url,
    )
    # Rows persist under the dispatch-canonical product (no-op for
    # aligned connectors; the VCF-family reconciliation).
    coords = _BatchCoordinates(
        tenant_id=tenant_id,
        product=_reconciled_row_product(product=product, version=version, impl_id=impl_id),
        version=version,
        impl_id=impl_id,
        spec_source=spec_source,
    )
    counts = await _run_upsert_loop(
        session=session,
        coords=coords,
        operations=operations,
        embedding_service=embedding_service,
    )
    return IngestionResult(
        inserted_count=counts["inserted"],
        updated_count=counts["updated"],
        skipped_count=counts["skipped"],
        connector_registered=connector_registered,
        operations_grouped=False,
    )


def _preflight_and_register_class(
    *,
    product: str,
    version: str,
    impl_id: str,
    base_url: str | None,
) -> bool:
    """Run the version-coverage pre-flight, then ensure the v2 class exists.

    G0.9-T9 (#741). The pre-flight confirms the operator's ``version``
    label is dispatchable against at least one already-registered class
    for ``(product, impl_id)`` — it must run **before** the auto-shim is
    synthesised, because the shim's ``supported_version_range`` is
    derived from the operator's own ``version`` and would make a
    post-shim check pass vacuously. Raises :exc:`UncoveredVersionLabel`
    (HTTP 422 at the router) when a class is registered but none accepts
    the label; logs ``connector_ingest_orphaned_class`` and proceeds when
    no class is registered yet (the v0.4-staging path).

    Both calls take the *supplied* (registry) product — not the
    dispatch-canonical one the rows persist under — so the coverage check
    finds the real registered class (e.g. ``VcfLogsConnector`` under
    ``product="vcf-logs"``) and no redundant shim is synthesised.

    Returns the ``connector_registered`` flag (``True`` when a fresh
    auto-shim was registered).
    """
    check_version_covered_by_registered_class(
        product=product,
        version=version,
        impl_id=impl_id,
    )
    return ensure_connector_class_registered(
        product=product,
        version=version,
        impl_id=impl_id,
        base_url=base_url,
    )


def _reconciled_row_product(*, product: str, version: str, impl_id: str) -> str:
    """Return the product the persisted rows must carry to be dispatchable.

    The dispatch/query surface keys on the product
    :func:`~meho_backplane.operations._lookup.parse_connector_id` derives
    from the connector_id, not the operator-supplied one. For aligned
    connectors the two are identical (no-op); for the VCF-family long↔short
    splits the supplied product (``vcf-logs``) diverges from the derived
    spelling (``vrli``), and rows written under the supplied product are
    invisible to every dispatch probe (the catalog reports
    ``registered, 0 ops``). The pre-flight + auto-shim registration in
    :func:`register_ingested_operations` deliberately stay on the *supplied*
    (registry) product so the version-coverage check finds the real
    ``VcfLogsConnector`` class and no redundant shim is synthesised; only
    the persisted rows are reconciled here. claude-rdc-hetzner-dc#1136.
    """
    row_product = dispatch_product(product=product, version=version, impl_id=impl_id)
    if row_product != product:
        _log.info(
            "ingested_rows_product_reconciled",
            supplied_product=product,
            row_product=row_product,
            version=version,
            impl_id=impl_id,
        )
    return row_product


async def _register_in_session(
    session: AsyncSession,
    *,
    coords: _BatchCoordinates,
    operations: Sequence[EndpointDescriptorProto],
    embedding_service: EmbeddingService | None,
    commit: bool,
) -> dict[str, int]:
    """Upsert every op in *operations* against *session*. Return action counts.

    Split out from :func:`register_ingested_operations` so the public
    function can branch on caller-owned-vs-helper-owned session
    without duplicating the loop body. ``commit=True`` commits after
    every op so a mid-batch crash leaves the already-processed rows
    persisted (the same shape T4's review-queue service uses);
    ``commit=False`` defers transaction boundaries to the caller.
    """
    log = structlog.get_logger(__name__)
    now = datetime.now(UTC)
    counts: dict[str, int] = {"inserted": 0, "updated": 0, "skipped": 0}

    for proto in operations:
        ctx = build_upsert_context(
            tenant_id=coords.tenant_id,
            product=coords.product,
            version=coords.version,
            impl_id=coords.impl_id,
            spec_source=coords.spec_source,
            proto=proto,
            now=now,
        )
        action = await upsert_one_operation(
            session,
            ctx,
            embedding_service=embedding_service,
        )
        counts[action] += 1
        if commit:
            await session.commit()
        log.debug(
            "ingested_operation_upserted",
            action=action,
            product=coords.product,
            version=coords.version,
            impl_id=coords.impl_id,
            op_id=proto.op_id,
            spec_source=coords.spec_source,
        )

    log.info(
        "ingested_operations_registered",
        product=coords.product,
        version=coords.version,
        impl_id=coords.impl_id,
        spec_source=coords.spec_source,
        **counts,
    )
    return counts


# Public surface intentionally limited to register_ingested_operations
# + IngestionResult (declared in __all__ at the top of the module). The
# LLM-grouping pass that assigns ops to operation_group rows is T3's
# job (#404) and runs as a separate step after this helper's bulk
# upsert; group_id stays NULL on every persisted row until then.
