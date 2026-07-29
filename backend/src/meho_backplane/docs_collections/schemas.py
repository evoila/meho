# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the doc-collections surface (G4.6 T1 #1550).

Two read models cover the registry's wire contract:

* :class:`DocCollection` — full read shape. Maps 1:1 to the
  ``doc_collections`` table columns. Returned by the resolver and (in
  T3 #1552) the collection-scoped search path. Frozen.
* :class:`DocCollectionSummary` — short shape for the catalogue list
  (``list_doc_collections``, T4 #1553). Carries the identification +
  routing-decision fields an agent needs to pick a collection
  (``collection_key`` / ``vendor`` / ``products`` / ``when_to_use``)
  plus the operator-facing liveness fields (``status`` /
  ``last_ingested_at`` / ``doc_count`` / ``readiness``). The
  ``backend`` record is **omitted** by design — the backend
  (``vertex-rag`` / ``meho-knowledge``) is resolved server-side and
  never appears in a catalogue response (#1548 backend-agnostic
  contract). Frozen.

:class:`DocCollectionCreate` is the write half (#1739): the request body
for ``POST /api/v1/doc_collections`` + the ``create_doc_collections`` MCP
tool + ``meho docs collections create``. It carries only the operator-set
identity + routing fields (``collection_key`` / ``vendor`` / ``products`` /
``backend`` + the optional ``description`` / ``when_to_use`` / ``extras``);
``id`` / ``tenant_id`` / timestamps / ``status`` / the probe-written
liveness are server-derived and deliberately absent — ``tenant_id`` in
particular comes from the JWT, never the body (the ``create_target``
precedent). There is still no ``DocCollectionUpdate`` here — PATCH is a
separate follow-up; DELETE shipped in #2487 as a bodyless route
(``DELETE /api/v1/doc_collections/{collection_key}``), so it needs no
request schema.

:class:`DocCollectionCreateResponse` is the create route's reply (#1756):
the full :class:`DocCollection` read shape plus a ``next_step`` hint that
names the ``create → probe → ready`` flow while the row is still
``provisioning``. A freshly-created collection defaults to
``provisioning`` and ``search_docs`` rejects a non-``ready`` collection,
so the only path to a searchable collection is an explicit
``POST /api/v1/doc_collections/{collection_key}/probe``; the hint points
the operator straight at it instead of leaving them to discover the probe
route after a confusing not-ready error. The hint is create-surface-only —
it is deliberately **not** on :class:`DocCollection` so the read shape the
collection-scoped search path and the MCP docs tools return stays a clean
1:1 mirror of the table.

:func:`project_doc_collection_to_summary` is the **single** ORM→wire
projection, mirroring :func:`targets.schemas.project_target_to_summary`.
Every surface that lists collections (the catalogue tool, the CLI
verb, the resolver's diagnostics) goes through it so list and detail
never drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meho_backplane.docs_collections.lifecycle import STATUS_PROVISIONING

if TYPE_CHECKING:
    from meho_backplane.db.models import DocCollection as DocCollectionORM

__all__ = [
    "DocCollection",
    "DocCollectionCreate",
    "DocCollectionCreateResponse",
    "DocCollectionSummary",
    "project_doc_collection",
    "project_doc_collection_create_response",
    "project_doc_collection_to_summary",
]


class DocCollectionBackend(BaseModel):
    """The ``{type, ref}`` backend routing record a create supplies.

    ``type`` is the search-backend type the row routes to (validated at
    the service layer against
    :func:`~meho_backplane.docs_search.backends.registry.all_backends` so
    an unroutable row is rejected at create time, not at probe time).
    ``ref`` is the per-collection backend config the resolved adapter
    reads (e.g. ``{"endpoint": "https://corpus/v1/search"}`` for
    ``corpus-http``); it is opaque to the create surface — the adapter
    owns its shape — so it is a free ``Mapping``.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    ref: Mapping[str, Any]


class DocCollectionCreate(BaseModel):
    """Request body for creating a doc collection (#1739).

    Carries only the operator-set identity + routing fields. The
    server derives ``id`` / ``tenant_id`` / ``created_at`` /
    ``updated_at`` / ``status`` and leaves the probe-written liveness
    (``last_ingested_at`` / ``doc_count`` / ``readiness``) NULL until the
    first probe — so none of those appear here. ``tenant_id`` is taken
    from the JWT in every front, never the body; a body carrying one is
    not modelled (``extra="forbid"`` rejects it) so a writer cannot even
    attempt a cross-tenant create. Mirrors
    :class:`~meho_backplane.targets.schemas.TargetCreate`.
    """

    model_config = ConfigDict(extra="forbid")

    collection_key: str = Field(min_length=1, max_length=128)
    vendor: str = Field(min_length=1, max_length=256)
    products: tuple[str, ...] = ()
    description: str | None = None
    when_to_use: str | None = None
    backend: DocCollectionBackend
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("products")
    @classmethod
    def _strip_empty_products(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank product tokens — a ``""`` entry is a typo, not a product."""
        if any(not p.strip() for p in value):
            msg = "products entries must be non-empty"
            raise ValueError(msg)
        return value


class DocCollection(BaseModel):
    """Full read shape — maps 1:1 to the ``doc_collections`` table.

    Frozen so callers can stash instances in request state or structured
    logs without fear of mutation. ``products`` is ``tuple[str, ...]``
    and the JSON columns are ``Mapping[str, Any]`` so a frozen instance
    cannot be mutated in-place via ``list.append`` / ``dict.__setitem__``.

    ``backend`` is the operator-set ``{type, ref}`` routing record the
    T2 (#1551) router resolves server-side. ``status`` is the lifecycle
    enum (``provisioning`` / ``ready`` / ``rebuilding`` / ``disabled``);
    ``last_ingested_at`` / ``doc_count`` / ``readiness`` are
    probe-written liveness (T6 #1555), ``None`` until the first probe.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    # NULL → global/shared collection; set → tenant-curated.
    tenant_id: UUID | None
    collection_key: str
    vendor: str
    products: tuple[str, ...]
    description: str | None
    when_to_use: str | None
    backend: Mapping[str, Any]
    status: str
    last_ingested_at: datetime | None
    doc_count: int | None
    readiness: Mapping[str, Any] | None
    extras: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


class DocCollectionCreateResponse(DocCollection):
    """The create route's reply: the full read shape plus a ``next_step`` hint.

    Extends :class:`DocCollection` with a single create-surface-only field,
    ``next_step`` (#1756). A create defaults ``status`` to ``provisioning``
    and ``search_docs`` rejects a non-``ready`` collection, so the operator
    must run an explicit ``POST /api/v1/doc_collections/{collection_key}/probe``
    before the collection is searchable. ``next_step`` carries that
    instruction inline so the ``create → probe → ready`` flow is
    discoverable from the create response itself, not only from a confusing
    not-ready error on the first search.

    ``next_step`` is ``None`` for any non-``provisioning`` create (no
    behaviour today creates a collection in another state, but the field is
    typed ``str | None`` so the contract is honest if one ever does). The
    field lives here, not on :class:`DocCollection`, so every read surface
    that returns the bare read shape (the collection-scoped search path, the
    ``list_doc_collections`` catalogue, the MCP docs tools) is unaffected.
    """

    next_step: str | None = None


class DocCollectionSummary(BaseModel):
    """Short shape for the catalogue list (``list_doc_collections``, T4).

    Carries the fields an agent needs to choose a collection before
    searching (``collection_key`` / ``vendor`` / ``products`` /
    ``when_to_use``) plus operator-facing liveness (``status`` /
    ``last_ingested_at`` / ``doc_count`` / ``readiness``). The
    ``backend`` record and free-form ``extras`` are deliberately omitted
    — the backend is server-side-only (#1548) and ``extras`` is an
    operator escape hatch that does not belong in the agent-facing
    catalogue. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID | None
    collection_key: str
    vendor: str
    products: tuple[str, ...]
    description: str | None
    when_to_use: str | None
    status: str
    last_ingested_at: datetime | None
    doc_count: int | None
    readiness: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime


def project_doc_collection(c: DocCollectionORM) -> DocCollection:
    """Project a :class:`DocCollectionORM` row to the full wire shape.

    The detail counterpart to :func:`project_doc_collection_to_summary` —
    the single ORM→:class:`DocCollection` projection the create route
    returns (#1739). Coerces the ORM's mutable ``list[str]`` ``products``
    column to the frozen schema's ``tuple[str, ...]`` so the returned
    instance is genuinely immutable; the JSON columns (``backend`` /
    ``readiness`` / ``extras``) ride through as ``Mapping``.
    """
    return DocCollection(
        id=c.id,
        tenant_id=c.tenant_id,
        collection_key=c.collection_key,
        vendor=c.vendor,
        products=tuple(c.products),
        description=c.description,
        when_to_use=c.when_to_use,
        backend=c.backend,
        status=c.status,
        last_ingested_at=c.last_ingested_at,
        doc_count=c.doc_count,
        readiness=c.readiness,
        extras=c.extras,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _next_step_for_status(status: str, collection_key: str) -> str | None:
    """Build the ``next_step`` create-response hint for *status* (#1756).

    A freshly-created collection is ``provisioning`` and ``search_docs``
    rejects a non-``ready`` collection, so the operator's next action is an
    explicit probe — return the route + key to call. Any other status (no
    create path produces one today) returns ``None``: there is no single
    obvious next step to advertise, and a stale hint is worse than none.
    """
    if status == STATUS_PROVISIONING:
        return (
            f"POST /api/v1/doc_collections/{collection_key}/probe to make "
            f"this collection searchable (it is provisioning until a probe "
            f"confirms its index is ready)."
        )
    return None


def project_doc_collection_create_response(
    c: DocCollectionORM,
) -> DocCollectionCreateResponse:
    """Project a created :class:`DocCollectionORM` row to the create reply (#1756).

    The create route's projection: the full :class:`DocCollection` read
    shape plus the ``next_step`` hint that names the ``create → probe →
    ready`` flow while the row is ``provisioning``. Reuses
    :func:`project_doc_collection` for the shared fields so the read shape
    never drifts between the two, then layers ``next_step`` on top.
    """
    base = project_doc_collection(c)
    return DocCollectionCreateResponse(
        **base.model_dump(),
        next_step=_next_step_for_status(c.status, c.collection_key),
    )


def project_doc_collection_to_summary(c: DocCollectionORM) -> DocCollectionSummary:
    """Project a :class:`DocCollectionORM` row to the wire summary shape.

    The single canonical ORM→wire projection for every surface that
    lists doc collections (the ``list_doc_collections`` catalogue tool
    in T4 #1553, the CLI verb, and the resolver's not-found
    diagnostics). Mirrors
    :func:`~meho_backplane.targets.schemas.project_target_to_summary`:
    one helper, one place to change, no list-vs-detail drift.

    Coerces ``products`` from the ORM column's mutable ``list[str]``
    JSON shape to the frozen schema's ``tuple[str, ...]`` so the
    returned :class:`DocCollectionSummary` is genuinely immutable.
    """
    return DocCollectionSummary(
        id=c.id,
        tenant_id=c.tenant_id,
        collection_key=c.collection_key,
        vendor=c.vendor,
        # ORM stores products as ``list[str]`` (mutable JSON column);
        # the wire schema declares ``tuple[str, ...]`` for frozen-model
        # immutability. Coerce at the boundary.
        products=tuple(c.products),
        description=c.description,
        when_to_use=c.when_to_use,
        status=c.status,
        last_ingested_at=c.last_ingested_at,
        doc_count=c.doc_count,
        readiness=c.readiness,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )
