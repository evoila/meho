# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``search_knowledge`` + ``add_to_knowledge`` — kb meta-tools (G4.1-T3).

Two of the ~17 agent-facing meta-tools defined by ``CLAUDE.md`` postulate 5.
Both call into :class:`~meho_backplane.kb.service.KbService` (G4.1-T1,
already on main via #430) and surface kb operations on the MCP transport;
the matching REST routes are owned by the sibling Task #416 and the CLI
verbs by #418 — every front-end converges on the same service layer.

Why this surface is load-bearing
================================

``search_knowledge`` is one of the most-called tools across every G3-G9
agent flow: every operator-driven workflow ("how did we restart vSphere
HA?", "what's the convention for tenant naming?") starts with a kb
lookup before an operation gets dispatched, and every kb write
(``add_to_knowledge``) is preceded by a search to avoid duplicates.
That call cadence makes the **tool descriptions** the single most
leveraged piece of agent UX in the kb surface. The descriptions below
follow the AI-engineering anchor: name what the tool does, when to use
it, when NOT to use it, and how output relates to the companion
``meho://kb/{slug}`` resource (so the agent learns the recipe
"search → pick → fetch full body via resources/read" instead of
inventing it).

RBAC + tenant scoping
=====================

Both tools require ``operator`` role and are not visible to
``read_only``. ``add_to_knowledge`` deliberately stops at ``operator``
(rather than escalating to ``tenant_admin`` like the REST
``POST /api/v1/kb`` route does in T2) because the agent surface is
intentionally narrow: an operator using the agent to capture
generalisable knowledge should be able to do so without an
out-of-band approval round-trip. The audit row + broadcast event the
dispatcher emits on every ``tools/call`` provide the traceability;
the REST route's stricter gate is for fleet-wide bulk imports the
agent surface does not perform.

Tenant scoping is enforced via :attr:`Operator.tenant_id` — every
:class:`KbService` call binds the tenant from the validated JWT
operator, so cross-tenant access is structurally impossible. The
substrate's tenant filter then carries the boundary down to the SQL
layer.

Audit + broadcast
=================

The dispatcher in :mod:`meho_backplane.mcp.handlers` emits one
``audit_log`` row + one broadcast event per ``tools/call`` invocation;
the ``op_class`` here (``"read"`` for search, ``"write"`` for add)
flows into :func:`~meho_backplane.broadcast.classify.classify_op` so
the broadcast payload is shaped correctly (full payload for write,
aggregate-only for credential-class reads; kb reads land in the
generic ``other`` class).

Error mapping
=============

``InvalidKbSlugError`` from :func:`~meho_backplane.kb.schemas.validate_slug`
maps to JSON-RPC ``-32602`` via :class:`McpInvalidParamsError` — the
operator passed a bad slug, which is invalid params, not a server-side
failure. Any other exception (DB error, embedding service failure,
etc.) bubbles to the dispatcher's generic catch and surfaces as
``-32603``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []


#: Op-class strings keep parity with :mod:`meho_backplane.broadcast.classify`'s
#: credential / audit / read / write taxonomy. ``search_knowledge`` is read;
#: ``add_to_knowledge`` is write.
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"

#: Default + maximum hit count for ``search_knowledge``. Matches the
#: :class:`KbService.search_entries` signature (default 10) and the
#: substrate cap (50, enforced inside
#: :func:`~meho_backplane.retrieval.retriever.retrieve`).
_DEFAULT_SEARCH_LIMIT: Final[int] = 10
_MAX_SEARCH_LIMIT: Final[int] = 50

#: Length cap on the ``search_knowledge`` free-text ``query``. Mirrors
#: the 256-char class the sibling tool slices use for free-text inputs
#: (``audit.py`` / ``topology.py``); both BM25 and the embedding encoder
#: consume the query, so an unbounded string is compute the substrate
#: pays for on every call.
_MAX_QUERY_CHARS: Final[int] = 256

#: Length cap on the ``add_to_knowledge`` Markdown body. Matches the
#: memory-surface cap (``tools/memory.py`` ``_MAX_BODY_CHARS`` and the
#: operator-console
#: :data:`meho_backplane.ui.routes.memory.views.BODY_MAX_LENGTH`) — the
#: same retrieval substrate indexes both corpora as single documents
#: (tsvector + 384-dim embedding), so the cap bounds the worst-case
#: per-call indexing allocation.
_MAX_BODY_CHARS: Final[int] = 64 * 1024

#: Shared slug-shape description rendered into both the tool input schema
#: (``add_to_knowledge``) and the resource template (``meho://kb/{slug}``).
#: Authored once so a future contract tweak edits one string. The
#: leading-letter rule is called out twice (in the prose AND in a paired
#: positive/negative example) because the v0.3.1 consumer dogfood
#: (G0.9.1-T8, Signal #15) hit -32602 on a digit-leading slug
#: ('657-recovery') without finding the rule in the schema description —
#: the regex catches it, but the example-of-an-invalid-slug here is
#: what makes the constraint discoverable before the call goes out.
_SLUG_DESCRIPTION: Final[str] = (
    "Operator-facing identifier in kebab-case, recommended shape "
    "'<product>-<version>-<topic>'. "
    "Must start with a lowercase letter, end with a lowercase letter or "
    "digit, and contain only lowercase letters, digits, hyphens, or dots. "
    "Valid example: 'vcenter-9.0-snapshot-revert'. "
    "Invalid example: '657-recovery' — slugs may not start with a digit; "
    "rewrite to 'ticket-657-recovery' or similar."
)


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _search_knowledge_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run a hybrid BM25 + cosine retrieval over the tenant's kb corpus.

    Delegates to :meth:`KbService.search_entries` with the operator's
    JWT-bound :attr:`~Operator.tenant_id`. The substrate enforces the
    tenant boundary at SQL level; there's no need for an in-handler
    re-check.

    The ``filters`` argument is forwarded verbatim — :class:`KbService`
    consumes the ``"kind"`` key and forwards every other key to the
    substrate's ``metadata_filters`` containment predicate
    (``documents.metadata @>`` translation, G4.4-T1 / #1177). Pass
    e.g. ``{"kind": "kb-entry", "source_kind": "evoila-distilled"}``
    to scope hits to the curated slice; missing keys exclude rows
    (PG ``@>`` semantics), multi-key dicts behave as an intersection.
    """
    query: str = arguments["query"]
    filters = arguments.get("filters")
    limit: int = int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT))

    service = KbService()
    hits = await service.search_entries(
        tenant_id=operator.tenant_id,
        query=query,
        filters=filters,
        limit=limit,
    )
    return {
        "hits": [hit.model_dump(mode="json") for hit in hits],
    }


async def _add_to_knowledge_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Create (or re-index) one kb entry under the operator's tenant.

    Three things this handler is responsible for:

    * Forwarding the operator's :attr:`~Operator.tenant_id` to the
      service — tenant scoping is at the call site, not in a contextvar
      resolver.
    * Catching :class:`InvalidKbSlugError` and re-raising as
      :class:`McpInvalidParamsError` so the dispatcher emits the
      spec-correct ``-32602`` for a bad slug rather than the generic
      ``-32603`` an uncaught exception would land on.
    * Returning the full :class:`~meho_backplane.kb.schemas.KbEntry`
      payload (id, slug, body, metadata, timestamps) so the agent can
      verify the write landed without a follow-up ``resources/read``.

    The body-hash short-circuit in
    :func:`~meho_backplane.retrieval.indexer.index_document` means a
    re-add of an unchanged body costs only an ``updated_at`` bump — no
    embedding compute — so the agent can call this freely as a
    "remember this" without worrying about re-embed overhead.
    """
    slug: str = arguments["slug"]
    body: str = arguments["body"]
    metadata = arguments.get("metadata")

    service = KbService()
    try:
        entry, _created = await service.create_entry(
            tenant_id=operator.tenant_id,
            slug=slug,
            body=body,
            metadata=metadata,
            actor_sub=operator.sub,
        )
    except InvalidKbSlugError as exc:
        raise McpInvalidParamsError(f"add_to_knowledge: {exc}") from exc

    return entry.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="search_knowledge",
        description=(
            "Search the tenant's knowledge base for distilled operator "
            "knowledge: vendor API patterns, lab conventions, "
            "known-good runbooks, post-incident learnings. Returns ranked "
            "hits with a slug, a ~200-char snippet, and metadata; "
            "ordered by a fused BM25 + cosine score. "
            "Call this BEFORE writing a new kb entry via "
            "`add_to_knowledge` to avoid duplicates, and BEFORE asking "
            "the operator a factual question about the lab — the answer "
            "may already be captured. "
            "For the FULL body of a hit (the snippet is truncated at "
            "200 chars with an ellipsis), call `resources/read` on "
            "`meho://kb/{slug}` with the hit's `slug` value. "
            'Pass `filters={"kind": "kb-entry"}` to narrow within '
            "the kb namespace; non-`kind` keys translate to "
            "Postgres `documents.metadata @>` containment "
            'against the row\'s metadata JSONB (e.g. `{"source_kind": '
            '"evoila-distilled"}` returns only curated entries, '
            "excluding vendor sidecars). Values must be JSON scalars; "
            "missing keys exclude rows (containment semantics). "
            "Limit defaults to 10; cap is 50 (substrate-enforced). "
            "Surface-name note: every non-MCP MEHO surface calls this "
            "substrate `kb`, not `knowledge` — REST is `/api/v1/kb` "
            "(`GET`/`POST /api/v1/kb`, `GET`/`DELETE /api/v1/kb/{slug}`), "
            "the CLI verb is `meho kb`, the console is `/ui/kb`. There is "
            "no `/api/v1/knowledge` route (it 404s). There is also no MCP "
            "delete tool: clean up entries via `DELETE /api/v1/kb/{slug}` "
            "(returns 204, tenant_admin) or `meho kb delete` — deletion is "
            "REST/CLI-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_QUERY_CHARS,
                    "description": (
                        "Free-form query. Both BM25 (lexical) and cosine "
                        "(semantic) signals consume it; ranks are fused via RRF. "
                        f"Capped at {_MAX_QUERY_CHARS} characters."
                    ),
                },
                "filters": {
                    "type": ["object", "null"],
                    "description": (
                        "Optional filter dict. The 'kind' key narrows "
                        "by document kind; non-'kind' keys translate to "
                        "Postgres `documents.metadata @>` containment "
                        "(missing keys exclude the row, multi-key "
                        "intersects). Values must be JSON scalars "
                        "(string / number / boolean / null)."
                    ),
                    "properties": {
                        "kind": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": True,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                    "default": _DEFAULT_SEARCH_LIMIT,
                    "description": "Maximum number of ranked hits to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_search_knowledge_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="add_to_knowledge",
        description=(
            "Add a new entry to the tenant's knowledge base. Use when "
            "you (or the operator working with you) learn something "
            "generalizable about a vendor API, an operational pattern, "
            "or a post-incident learning that's worth retaining for the "
            "team. The body is Markdown — feel free to use code fences, "
            "lists, and headings. "
            "ALWAYS call `search_knowledge` FIRST with the topic you're "
            "about to capture: re-adding under a different slug fragments "
            "the corpus and dilutes future retrieval quality. If a "
            "matching entry already exists, prefer extending its body "
            "(read via `meho://kb/{slug}`, then re-add with the merged "
            "body — the same-slug re-add updates in place via the "
            "body-hash short-circuit). "
            "Do NOT use this for ephemeral session notes — those belong "
            "in `add_to_memory` (G5). The kb is durable team knowledge. "
            "Surface-name note: every non-MCP MEHO surface calls this "
            "substrate `kb`, not `knowledge` — REST is `/api/v1/kb` "
            "(`GET`/`POST /api/v1/kb`, `GET`/`DELETE /api/v1/kb/{slug}`), "
            "the CLI verb is `meho kb`, the console is `/ui/kb`. There is "
            "no `/api/v1/knowledge` route (it 404s). To DELETE an entry "
            "there is no MCP tool: use `DELETE /api/v1/kb/{slug}` (returns "
            "204, tenant_admin) or `meho kb delete` — deletion is "
            "REST/CLI-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "description": _SLUG_DESCRIPTION,
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_BODY_CHARS,
                    "description": (
                        "Markdown body. Stored as-is; no chunking. The "
                        "retrieval substrate indexes it as a single "
                        "document (BM25 over tsvector + 384-dim "
                        "embedding for cosine). "
                        f"Capped at {_MAX_BODY_CHARS} characters."
                    ),
                },
                "metadata": {
                    "type": ["object", "null"],
                    "description": (
                        "Optional JSON-shaped metadata. Null or omitted "
                        "preserves any existing row's metadata on "
                        "re-index; pass an empty object {} to clear. "
                        "Free-form keys; the retrieval surface does not "
                        "filter on metadata in v0.2."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["slug", "body"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_add_to_knowledge_handler,
)
