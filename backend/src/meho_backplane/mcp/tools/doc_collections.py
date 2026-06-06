# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``list_doc_collections`` — the catalogue-discovery MCP tool (G4.6-T4 #1553).

The docs analogue of ``list_targets`` (``mcp/tools/topology.py``):
``list_targets`` answers "what infra can I act on?"; this tool answers
"what docs can I search?". An agent calls it to learn which
``collection`` keys it may pass to ``search_docs`` / ``ask_docs`` *before*
it searches, picking the right corpus from the ``vendor`` / ``products`` /
``when_to_use`` blurb rather than guessing a key.

Two gates, matching ``search_docs``
====================================

* ``required_capability="meho-docs"`` (G4.5-T1 #1519) — a tenant that has
  not provisioned the add-on never sees the tool in ``tools/list`` (true
  absence) and a direct ``tools/call`` is rejected before the handler
  runs. The registry + dispatcher own this enforcement; the tool only
  declares the gate.
* **Per-collection entitlement** — the static gate above governs
  *visibility*; this finer filter governs *which collections* an entitled
  tenant actually sees in the catalogue. The handler returns only the
  collections the operator holds ``meho-docs:<collection_key>`` for
  (:func:`~meho_backplane.docs_search.collection_capability_key`), the
  same per-collection key ``search_docs`` enforces at query time. A
  catalogue that listed collections the agent could not then search would
  be a foot-gun: every "pick from list_doc_collections" hint would point
  at keys ``search_docs`` rejects with 403.

Tenant scope + pagination
==========================

Reads the ``doc_collections`` table tenant-scoped — global / shared rows
(``tenant_id IS NULL``) plus this tenant's own curated rows — the same
``(tenant_id IS NULL) OR (tenant_id = :tenant)`` visibility the resolver
(:func:`~meho_backplane.docs_collections.resolve_doc_collection`) enforces.
A tenant-curated row that shadows a global key under the same
``collection_key`` is de-duplicated so the catalogue answer is "this key
exists", not "in how many scopes" — the tenant row wins (it overrides the
global backend binding / metadata), mirroring the resolver's
tenant-then-global preference.

Keyset-paginated by ``collection_key`` (the stable agent-facing id), the
same lexicographic-cursor shape ``list_targets`` paginates ``name`` by:
``cursor`` is the last key on the previous page, ``next_cursor`` is the
last key on a full page (more rows may exist) or ``null`` on a short page
(definitively the end).

Audit
=====

The dispatcher writes one ``audit_log`` row per ``tools/call``; the
handler binds ``audit_op_id="meho.docs.collections.list"`` so a
who-touched / ``query_audit`` filter on ``op_id="meho.docs.*"`` catches
the catalogue read alongside ``meho.docs.search`` / ``meho.docs.ask``
across REST + CLI + MCP (the canonical-op_id discipline G4.5-T8 #1549
established). ``op_class="read"`` — the catalogue read never mutates the
registry.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import project_doc_collection_to_summary
from meho_backplane.docs_search import collection_capability_key
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool

__all__: list[str] = []


#: The capability key a tenant must have provisioned to see / call
#: ``list_doc_collections``. Same ``meho-docs`` add-on key the sibling
#: ``search_docs`` / ``ask_docs`` tools gate on (G4.5-T1 #1519).
_DOCS_CAPABILITY: Final[str] = "meho-docs"

#: Read op-class — the catalogue listing never mutates the registry.
_OP_CLASS_READ: Final[str] = "read"

#: Canonical audit op_id — the SAME ``meho.docs.*`` family the REST route
#: and CLI verb bind, so a ``query_audit`` filter on ``op_id="meho.docs.*"``
#: catches the catalogue read transport-independently (G4.5-T8 #1549).
_LIST_OP_ID: Final[str] = "meho.docs.collections.list"

#: Default + maximum page size. Generous default (the catalogue is small —
#: one row per corpus) with a hard cap so a client cannot ask for an
#: unbounded page.
_DEFAULT_LIMIT: Final[int] = 100
_MAX_LIMIT: Final[int] = 500


_LIST_DOC_COLLECTIONS_DESCRIPTION: Final[str] = (
    "List the documentation collections you are entitled to search — the "
    "catalogue an agent reads to learn WHICH `collection` to pass to "
    "`search_docs` / `ask_docs` before searching. Each row carries the "
    "`collection_key` (what those tools expect as their `collection`), the "
    "`vendor` and `products` the corpus covers, a `when_to_use` blurb for "
    "picking the right one, and operator-facing liveness "
    "(`status` / `doc_count` / `last_ingested_at`).\n\n"
    "WHEN TO CALL: before the first `search_docs` / `ask_docs` of a "
    "session, or whenever you have a vendor question but do not yet know "
    "which collection key holds the answer ('does any collection cover "
    "NSX?'). Only collections you are entitled to search appear — a key "
    "listed here is one `search_docs` will accept, not reject.\n\n"
    "Optional `vendor` narrows the catalogue to one vendor (exact match). "
    "Returns `{collections: [{id, collection_key, vendor, products, "
    "description, when_to_use, status, doc_count, last_ingested_at}, ...], "
    "next_cursor: <collection_key|null>}` ordered by `collection_key`; "
    "`next_cursor` is the last key on the page when more rows may exist, "
    "else null."
)


_LIST_DOC_COLLECTIONS_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vendor": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "OPTIONAL exact-match vendor filter (e.g. 'VMware by "
                "Broadcom'). Narrows the catalogue to one vendor; omit it "
                "to list every collection you are entitled to."
            ),
        },
        "cursor": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Keyset pagination cursor — the `collection_key` of the "
                "last row from the previous page. Omit it for the first "
                "page; pass the previous response's `next_cursor` to "
                "fetch the next page."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_LIMIT,
            "default": _DEFAULT_LIMIT,
            "description": "Maximum number of collections to return per page.",
        },
    },
    "additionalProperties": False,
}


_COLLECTION_OUTPUT_ITEM: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "collection_key": {"type": "string"},
        "vendor": {"type": "string"},
        "products": {"type": "array", "items": {"type": "string"}},
        "description": {"type": ["string", "null"]},
        "when_to_use": {"type": ["string", "null"]},
        "status": {"type": "string"},
        "doc_count": {"type": ["integer", "null"]},
        "last_ingested_at": {"type": ["string", "null"]},
    },
    "required": [
        "id",
        "collection_key",
        "vendor",
        "products",
        "description",
        "when_to_use",
        "status",
        "doc_count",
        "last_ingested_at",
    ],
}


def _summary_to_wire(row: DocCollectionORM) -> dict[str, Any]:
    """Project a row to the catalogue wire shape via the canonical summary.

    Goes through
    :func:`~meho_backplane.docs_collections.project_doc_collection_to_summary`
    — the single ORM→wire projection every collection-listing surface
    shares — then drops the summary's internal-only ``tenant_id`` /
    ``created_at`` / ``updated_at`` from the agent-facing catalogue
    response. The ``backend`` record is already absent from the summary by
    design (#1548 backend-agnostic contract). ``last_ingested_at`` is
    serialised to its ISO-8601 string (or ``None``) so the JSON-RPC result
    is a plain dict the dispatcher can emit without a pydantic round-trip.
    """
    summary = project_doc_collection_to_summary(row)
    return {
        "id": str(summary.id),
        "collection_key": summary.collection_key,
        "vendor": summary.vendor,
        "products": list(summary.products),
        "description": summary.description,
        "when_to_use": summary.when_to_use,
        "status": summary.status,
        "doc_count": summary.doc_count,
        "last_ingested_at": (
            summary.last_ingested_at.isoformat() if summary.last_ingested_at is not None else None
        ),
    }


def _dedupe_tenant_first(rows: list[DocCollectionORM]) -> list[DocCollectionORM]:
    """Collapse global+tenant rows sharing a ``collection_key``, tenant wins.

    A ``collection_key`` may exist both as a global row
    (``tenant_id IS NULL``) and as a tenant-curated row; the dual partial
    unique indexes let both coexist (migration 0037). The resolver prefers
    the tenant row (it overrides the global backend binding / metadata), so
    the catalogue must too — listing both would show the same key twice and
    let the agent pick the shadowed global metadata. Rows arrive ordered by
    ``collection_key`` then ``tenant_id`` (NULLs first under SQLite/PG
    ``NULLS FIRST`` default for ASC), so the tenant row is the *second*
    occurrence of a duplicated key and overwrites the global one in the
    per-key map; insertion order is preserved for keys that appear once.
    """
    by_key: dict[str, DocCollectionORM] = {}
    for row in rows:
        existing = by_key.get(row.collection_key)
        # Keep the tenant-curated row over a global one for the same key.
        if existing is None or row.tenant_id is not None:
            by_key[row.collection_key] = row
    return list(by_key.values())


async def _list_doc_collections_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the catalogue of collections the operator is entitled to search.

    Reads ``doc_collections`` tenant-scoped (global + this tenant's rows),
    de-duplicates a shadowed global key in favour of the tenant row, and
    filters to the collections the operator holds
    ``meho-docs:<collection_key>`` for — the same per-collection entitlement
    ``search_docs`` enforces, so every listed key is one ``search_docs``
    will accept. Keyset-paginated by ``collection_key``.

    The entitlement filter runs in Python (not the SQL WHERE) because the
    entitlement lives in the operator's capability set, not a joinable
    column; the catalogue is small (one row per corpus) so the over-read is
    negligible and keeps the keyset cursor a single column.
    """
    # Bind the canonical op_id so the persisted audit row is filterable by
    # ``op_id="meho.docs.*"`` the same way the search faces are (G4.5-T8
    # #1549). Bound up-front so a handler exception still records the
    # canonical identity. ``op_class="read"`` is declared on the
    # ToolDefinition; the broadcast / classify_op op_id stays the bare tool
    # name, so read-class broadcast sensitivity is unchanged.
    structlog.contextvars.bind_contextvars(audit_op_id=_LIST_OP_ID)

    vendor: str | None = arguments.get("vendor")
    cursor: str | None = arguments.get("cursor")
    limit = int(arguments.get("limit", _DEFAULT_LIMIT))

    stmt = select(DocCollectionORM).where(
        (DocCollectionORM.tenant_id == operator.tenant_id) | (DocCollectionORM.tenant_id.is_(None)),
    )
    if vendor is not None:
        stmt = stmt.where(DocCollectionORM.vendor == vendor)
    if cursor is not None:
        stmt = stmt.where(DocCollectionORM.collection_key > cursor)
    # Order by ``collection_key`` (the cursor + dedupe key) so the keyset
    # pagination is deterministic. The secondary ``tenant_id`` ordering is a
    # tie-break only — ``_dedupe_tenant_first`` is order-independent (the
    # tenant row always wins regardless of which arrives first), so the
    # NULLS-FIRST/LAST dialect difference between SQLite and PostgreSQL does
    # not affect which scope's row survives a shadowed key.
    stmt = stmt.order_by(
        DocCollectionORM.collection_key,
        DocCollectionORM.tenant_id,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    deduped = _dedupe_tenant_first(rows)
    entitled = [
        row
        for row in deduped
        if collection_capability_key(row.collection_key) in operator.capabilities
    ]
    # Page after the full tenant-scoped read so the keyset cursor advances
    # over the *entitled* set the agent actually sees: a page of `limit`
    # entitled rows whose last key is the cursor for the next page. Slicing
    # post-filter keeps the cursor stable even when interleaved
    # not-entitled rows sit between two entitled keys.
    page = entitled[:limit]
    collections = [_summary_to_wire(row) for row in page]
    next_cursor = collections[-1]["collection_key"] if len(page) == limit else None
    return {"collections": collections, "next_cursor": next_cursor}


register_mcp_tool(
    definition=ToolDefinition(
        name="list_doc_collections",
        description=_LIST_DOC_COLLECTIONS_DESCRIPTION,
        inputSchema=_LIST_DOC_COLLECTIONS_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "collections": {
                    "type": "array",
                    "items": _COLLECTION_OUTPUT_ITEM,
                },
                "next_cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "Last collection_key on the page when more rows may "
                        "exist; null when the page is the end of the set."
                    ),
                },
            },
            "required": ["collections", "next_cursor"],
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_list_doc_collections_handler,
)
