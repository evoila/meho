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

There is no ``DocCollectionCreate`` / ``DocCollectionUpdate`` here: v1
collections are operator-managed seed (no create/import API until a
collection needs one — out of scope per #1550). When that API lands it
adds the write schemas alongside these read models.

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

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from meho_backplane.db.models import DocCollection as DocCollectionORM

__all__ = [
    "DocCollection",
    "DocCollectionSummary",
    "project_doc_collection_to_summary",
]


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
