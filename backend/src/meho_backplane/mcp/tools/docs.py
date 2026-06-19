# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``search_docs`` / ``ask_docs`` — capability-gated vendor-document tools.

The MCP face of the federated vendor-document corpus the ops team runs
(Initiative #1518, the ``meho-docs`` add-on). Two sibling tools share the
same gate, the same REQUIRE_FILTERS posture, and the same shared
:func:`~meho_backplane.docs_search.search_docs` retrieval service:

* ``search_docs`` (G4.5-T4, #1523) — returns the ranked **cited chunks**.
  The third consumer of the shared service alongside the REST route (T3,
  #1521) and the CLI verb (T5, #1524).
* ``ask_docs`` (G4.5-T7, #1526) — the synthesis fast-follow: runs the
  *same* retrieval, then composes a single **grounded, cited answer** over
  those chunks via :func:`~meho_backplane.docs_search.synthesize_docs_answer`
  and returns ``{answer, citations[]}``. No claim without a citation; an
  empty retrieval returns "no grounded answer", never a hallucinated one;
  an unconfigured synthesis model fails closed (``-32603``, the MCP
  analogue of 503). It is read-class — it composes over retrieved chunks,
  it never mutates the corpus — so it keeps ``op_class="read"``.

Defining both here keeps the REQUIRE_FILTERS posture and the cited-chunk
shape in one place, never re-derived per surface.

Capability gate (vs. the role gate)
===================================

Unlike every kb / memory meta-tool — gated by ``required_role`` alone —
``search_docs`` carries a second, orthogonal gate:
``required_capability="meho-docs"`` (G4.5-T1, #1519). A tenant that has
not provisioned the ``meho-docs`` add-on never sees the tool in
``tools/list`` (true absence, not a greyed-out entry) and a ``tools/call``
naming it directly is rejected with a 403-class error before the handler
runs. The gate is enforced twice — once at list time
(:func:`~meho_backplane.mcp.registry.all_tools_for`) and once at call
time (:func:`~meho_backplane.mcp.handlers.handle_tools_call`) — so
learning the name out-of-band cannot bypass it. This module only declares
the gate; the registry + dispatcher own the enforcement.

REQUIRE_FILTERS surfaces as an MCP error
========================================

``product`` and ``version`` are a **mandatory binary scope**, not a hint.
The handler calls :func:`~meho_backplane.docs_search.build_docs_scope`,
which raises :class:`~meho_backplane.docs_search.MissingDocsFilterError`
when the REQUIRE_FILTERS gate is on and either is blank. The route renders
that as HTTP 422; here it maps to :class:`McpInvalidParamsError`
(JSON-RPC ``-32602``) — the MCP analogue of a 422, since a missing
mandatory scope is invalid params, not a server fault. The inputSchema
already declares both as ``required``, so a well-behaved client never
reaches the service-side check; the map exists for the gate-off →
gate-on settings flip and for clients that skip schema validation.

Corpus-unavailable surfaces as an internal error
================================================

A federated corpus that is unconfigured, unreachable, or returns a
non-2xx / malformed response raises the typed
:class:`~meho_backplane.auth.corpus.CorpusUnavailable` from the transport.
This is **not** invalid params — the operator's request was well-formed;
the upstream is down — so it is *not* caught here. It bubbles to the
dispatcher's generic catch and surfaces as JSON-RPC ``-32603`` Internal
Error (the MCP analogue of the route's 503). The transport guarantees the
corpus response body is never on the exception, so nothing leaks through
the error message.

Audit + tenant scoping
======================

The dispatcher in :mod:`meho_backplane.mcp.handlers` writes exactly one
``audit_log`` row per ``tools/call`` with the ``op_class="read"`` declared
below, and hashes the raw arguments into ``params_hash`` — so the query is
recorded only as a hash, never in the clear, matching the route's
``meho.docs.search`` privacy posture. The op_id on that row is the
**canonical, uniform** ``meho.docs.search`` / ``meho.docs.ask`` — the same
token the REST route and the CLI verb bind (G4.5-T8 #1549) — so a
who-touched / ``query_audit`` filter on ``op_id="meho.docs.*"`` is
transport-independent and catches the MCP face (the primary agent surface)
alongside REST + CLI. Each handler binds it via the ``audit_op_id``
contextvar, which the dispatcher lifts into the persisted row's payload
op_id. The bare tool name (``search_docs`` / ``ask_docs``) is still what
the broadcast path passes to ``classify_op``, so the read-class broadcast
sensitivity is unchanged — only the persisted audit identity is unified.
Tenant scoping rides the operator's forwarded JWT: the service hands
``operator.raw_jwt`` to the corpus, which authenticates and audits the
call as the operator; there is no tool argument that names a tenant.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    CollectionDisabledError,
    CollectionForbiddenError,
    CollectionScope,
    ConflictingCollectionScopeError,
    DocsScope,
    DocsSearchResult,
    MissingDocsFilterError,
    NoEntitledReadyCollectionError,
    UnknownCollectionError,
    build_docs_scope,
    expand_docs_query,
    parse_collection_scope,
    resolve_entitled_ready_collection,
    resolve_entitled_ready_collections,
    retrieve_multi_query,
    search_docs,
    search_docs_fanout,
    synthesize_docs_answer,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []


#: The capability key a tenant must have provisioned to see / call
#: ``search_docs``. Matches the ``meho-docs`` add-on name (Initiative
#: #1518) and the key the JWT capability claim carries.
_DOCS_CAPABILITY: Final[str] = "meho-docs"

#: Read op-class — parity with :mod:`meho_backplane.broadcast.classify`'s
#: taxonomy and with the route's ``audit_op_class="read"``. The raw query
#: never reaches the broadcast feed (the dispatcher publishes only the
#: hashed ``params_hash``), so ``read``'s full-detail broadcast is safe.
_OP_CLASS_READ: Final[str] = "read"

#: Default + maximum hit count. Mirrors the route's
#: :class:`SearchDocsRequest` bounds (``default=10``, ``le=50``) so the
#: three consumers of the shared service agree on the cap.
_DEFAULT_SEARCH_LIMIT: Final[int] = 10
_MAX_SEARCH_LIMIT: Final[int] = 50

#: Canonical audit op_ids — the SAME tokens the REST route binds
#: (:func:`meho_backplane.api.v1.search_docs` binds ``meho.docs.search``)
#: and the CLI verb carries, so a who-touched / ``query_audit`` filter on
#: ``op_id="meho.docs.*"`` is transport-independent across REST / CLI / MCP
#: (G4.5-T8 #1549). Bound into the ``audit_op_id`` contextvar, which the
#: dispatcher lifts into the persisted ``audit_log.payload`` op_id; the
#: broadcast / ``classify_op`` op_id stays the bare tool name, so the
#: read-class broadcast sensitivity is unchanged.
_SEARCH_OP_ID: Final[str] = "meho.docs.search"
_ASK_OP_ID: Final[str] = "meho.docs.ask"


def _build_scope_or_invalid_params(tool: str, arguments: dict[str, Any]) -> DocsScope:
    """Build the binary scope from *arguments* or raise ``-32602``.

    ``collection`` is the mandatory binary scope (T3 #1552); ``product`` /
    ``version`` are optional refinements. A missing/blank ``collection``
    raises :class:`MissingDocsFilterError`, re-raised here as
    :class:`McpInvalidParamsError` so the dispatcher emits the spec-correct
    ``-32602`` (the MCP analogue of the route's 422). ``collection`` is no
    longer in the inputSchema's ``required`` list (the fan-out
    ``collections`` is the alternative scope, T5 #1554), so ``.get`` rather
    than indexing — the missing-scope case is the same ``-32602`` here as
    when an empty string is passed.
    """
    collection: str | None = arguments.get("collection")
    product: str | None = arguments.get("product")
    version: str | None = arguments.get("version")
    try:
        return build_docs_scope(collection, product, version)
    except MissingDocsFilterError as exc:
        raise McpInvalidParamsError(f"{tool}: {exc}") from exc


async def _resolve_collection_or_error(
    operator: Operator,
    scope: DocsScope,
    *,
    tool: str,
) -> DocCollection:
    """Resolve + entitle + readiness-check the scoped collection.

    Opens its own DB session (the MCP dispatcher does not thread one), runs
    the shared :func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`
    gate, and maps the typed access errors onto the MCP wire:

    * **Unknown collection** → :class:`McpInvalidParamsError` (``-32602``):
      a ``collection`` argument naming no visible collection is invalid
      params, not a server fault. The catalogue of visible keys rides
      ``error.data`` so the agent can self-correct.
    * **Not entitled** → :class:`McpInvalidParamsError` (``-32602``,
      projected to the 403-class audit status by the dispatcher): the same
      403-projected path the static capability gate uses. The collection
      exists; the tenant lacks ``meho-docs:<collection>``.
    * **Disabled** — :class:`~meho_backplane.docs_search.CollectionDisabledError`
      → :class:`McpInvalidParamsError` (``-32602``): an operator hid the
      collection from service. This is a **terminal**, client-actionable
      rejection (the agent must not retry), so it maps to the spec's
      "invalid params" lane like the entitlement miss — distinct from the
      retryable not-ready ``-32603`` below.
    * **Not ready** — :class:`~meho_backplane.docs_search.CollectionNotReadyError`
      is *not* caught here: a known + entitled collection that is
      transiently ``provisioning`` / ``rebuilding`` is a server-side,
      **retryable** condition (the MCP analogue of the route's 409/503), so
      it bubbles to the dispatcher's generic catch as ``-32603``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            return await resolve_entitled_ready_collection(session, operator, scope.collection_key)
        except UnknownCollectionError as exc:
            raise McpInvalidParamsError(
                f"{tool}: unknown collection {exc.collection_key!r}",
                data={"known_collections": exc.known_keys},
            ) from exc
        except CollectionForbiddenError as exc:
            # ``str(exc)`` already names the missing capability + the identity
            # it checked; also surface the capability key on ``error.data`` so
            # an agent can self-correct without parsing the message (T2 #1802).
            raise McpInvalidParamsError(
                f"{tool}: {exc}",
                data={
                    "reason": "not_entitled",
                    "required_capability": exc.required_capability,
                },
            ) from exc
        except CollectionDisabledError as exc:
            raise McpInvalidParamsError(
                f"{tool}: {exc}",
                data={"reason": "collection_disabled"},
            ) from exc


def _parse_scope_or_invalid_params(tool: str, arguments: dict[str, Any]) -> CollectionScope:
    """Parse the single / fan-out collection scope or raise ``-32602``.

    Maps :class:`ConflictingCollectionScopeError` (both ``collection`` and
    ``collections`` supplied) to :class:`McpInvalidParamsError` — the MCP
    analogue of the route's 422 for mutually-exclusive scopes.
    """
    collection = arguments.get("collection")
    collections = arguments.get("collections")
    try:
        return parse_collection_scope(collection, collections)
    except ConflictingCollectionScopeError as exc:
        raise McpInvalidParamsError(f"{tool}: {exc}") from exc


async def _resolve_fanout_or_error(
    operator: Operator,
    requested_keys: list[str] | None,
    *,
    tool: str,
) -> list[DocCollection]:
    """Resolve a fan-out scope's entitled, ready set or raise ``-32602``.

    Opens its own DB session (the MCP dispatcher does not thread one) and
    maps the empty-set failure: a fan-out that resolves to no entitled,
    ready collection is :class:`McpInvalidParamsError` (``-32602``, projected
    to the 403-class audit status by the dispatcher) — the same path the
    single-collection not-entitled arm uses. Non-entitled / not-ready
    members are dropped (logged) inside the resolver, not raised here.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            return await resolve_entitled_ready_collections(
                session, operator, requested_keys=requested_keys
            )
        except NoEntitledReadyCollectionError as exc:
            raise McpInvalidParamsError(f"{tool}: {exc}") from exc


async def _search_docs_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Route a vendor-document query through the shared docs-search service.

    Two scopes, mutually exclusive: a single ``collection`` (the T3 path) or
    a cross-collection fan-out (``collections=[…]`` / ``collection="all"``,
    T5 #1554) that RRF-merges across every entitled, ready collection.
    Forwards the operator's JWT so each backend authenticates and audits the
    call as the operator.

    Error arms: a conflicting (both single + fan-out) scope, a missing/blank
    or unknown / not-entitled / ``disabled`` single ``collection``, or a
    fan-out that resolves to no entitled, ready collection → ``-32602``
    (all client-actionable, terminal — a disabled collection carries
    ``error.data.reason='collection_disabled'``); a *transiently* not-ready
    single collection (``provisioning`` / ``rebuilding``) or an unavailable
    :class:`~meho_backplane.auth.corpus.CorpusUnavailable` backend bubbles to
    ``-32603`` (a well-formed request against a backend that is down / not
    serving yet is a server-side, retryable fault, not invalid params).
    """
    # Bind the canonical op_id so the persisted audit row is filterable by
    # ``op_id="meho.docs.search"`` the same way the REST + CLI faces are
    # (G4.5-T8 #1549). ``audit_collection`` is bound per-path below. Bound
    # up-front so a handler exception still records the canonical identity.
    structlog.contextvars.bind_contextvars(audit_op_id=_SEARCH_OP_ID)
    query: str = arguments["query"]
    limit: int = int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT))

    scope = _parse_scope_or_invalid_params("search_docs", arguments)
    if scope.is_fanout():
        result = await _run_search_fanout(operator, query, scope, limit)
    else:
        result = await _run_search_single("search_docs", operator, arguments, query, limit)
    return {
        "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
    }


async def _run_search_single(
    tool: str,
    operator: Operator,
    arguments: dict[str, Any],
    query: str,
    limit: int,
) -> DocsSearchResult:
    """The single-collection path (T3 #1552): one backend, one collection."""
    scope = _build_scope_or_invalid_params(tool, arguments)
    structlog.contextvars.bind_contextvars(audit_collection=scope.collection_key)
    collection = await _resolve_collection_or_error(operator, scope, tool=tool)
    return await search_docs(operator, query, scope=scope, collection=collection, limit=limit)


async def _run_search_fanout(
    operator: Operator,
    query: str,
    scope: CollectionScope,
    limit: int,
) -> DocsSearchResult:
    """The cross-collection fan-out path (T5 #1554): RRF over entitled set.

    Binds ``audit_collection`` to the **sorted, comma-joined** queried set
    (the resolver returns the collections sorted) so who-touched attributes
    the fan-out to every collection it touched.
    """
    collections = await _resolve_fanout_or_error(
        operator, scope.requested_keys(), tool="search_docs"
    )
    structlog.contextvars.bind_contextvars(
        audit_collection=",".join(c.collection_key for c in collections)
    )
    return await search_docs_fanout(operator, query, collections=collections, limit=limit)


register_mcp_tool(
    definition=ToolDefinition(
        name="search_docs",
        description=(
            "Search a vendor-document collection (product manuals, KB "
            "articles, design / reference guides) for an authoritative "
            "vendor fact — e.g. 'NSX config maximums for 9.0' or "
            "'vCenter 8.0 supported snapshot depth'. "
            "REQUIRES a collection scope: EITHER a single `collection` (the "
            "hard binary scope naming WHICH corpus to search and gating "
            "entitlement — pick it from `list_doc_collections`) OR a "
            "cross-collection fan-out via `collections` (an explicit list of "
            "keys) or `collection`='all' (every collection you are entitled "
            "to). A fan-out queries each collection independently and merges "
            "the hits by reciprocal-rank fusion, tagging each chunk with its "
            "source `collection`; use it only when you genuinely do not know "
            "which collection holds the answer (a single `collection` is "
            "cheaper and sharper). `collection` and `collections`/'all' are "
            "mutually exclusive. "
            "`product` and `version` are OPTIONAL refinements within a "
            "single collection (ignored on a fan-out), not a ranking hint. "
            "Use this for VENDOR REFERENCE — what the documentation says. "
            "Use `search_knowledge` instead for how THIS team does "
            "something (lab conventions, known-good runbooks, "
            "post-incident learnings), and `search_memory` for "
            "cross-session state (what you or the operator established "
            "earlier in this or a prior session). "
            "Returns ranked cited chunks: each carries the chunk text, a "
            "`source_url` citation, a `chunk_id`, and a `document_id`. "
            "For the full text of a hit on a later turn (when you kept "
            "only the citation), read `meho://docs/{collection}/{product}/"
            "{version}/{chunk_id}` via `resources/read`. "
            "Limit defaults to 10; cap is 50."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "Free-form vendor-reference query. Forwarded to each "
                        "collection's backend verbatim; never logged in the "
                        "clear (the audit row stores only its SHA-256 hash)."
                    ),
                },
                "collection": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Single collection key to search (e.g. 'vmware'), OR "
                        "the sentinel 'all' to fan out across every collection "
                        "you are entitled to. The binary scope — it routes the "
                        "query and gates per-collection entitlement. A query "
                        "with NEITHER `collection` NOR `collections` is "
                        "rejected with INVALID_PARAMS; supplying BOTH "
                        "`collection` and `collections` is also INVALID_PARAMS "
                        "(mutually exclusive). Pick keys from "
                        "`list_doc_collections`."
                    ),
                },
                "collections": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    "minItems": 1,
                    "maxItems": 64,
                    "description": (
                        "OPTIONAL cross-collection fan-out: an explicit list of "
                        "collection keys to query independently and merge by "
                        "reciprocal-rank fusion (each returned chunk is tagged "
                        "with its source `collection`). Mutually exclusive with "
                        "a single `collection`; equivalent to `collection`='all' "
                        "but scoped to the named keys. Non-entitled or not-ready "
                        "keys are dropped from the set."
                    ),
                },
                "product": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "OPTIONAL vendor-product refinement within a SINGLE "
                        "collection (e.g. 'nsx', 'vcenter'). Narrows the "
                        "search; omit it to search the whole collection. "
                        "Ignored on a cross-collection fan-out."
                    ),
                },
                "version": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "OPTIONAL product-version refinement (e.g. '9.0') "
                        "within a SINGLE collection. Narrows the search "
                        "alongside `product`; omit it to search the whole "
                        "collection. Ignored on a cross-collection fan-out."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                    "default": _DEFAULT_SEARCH_LIMIT,
                    "description": (
                        "Maximum number of ranked cited chunks to return. On a "
                        "fan-out this also caps the per-collection request "
                        "before the merge."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_search_docs_handler,
)


async def _ask_docs_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Answer a vendor-document question with a grounded, cited answer.

    The synthesis fast-follow to ``search_docs``, with a corpus-aware
    **expand** step in front of retrieval (#1916). The pipeline is:

    1. **Expand** — :func:`~meho_backplane.docs_search.expand_docs_query`
       rewrites the question into a small set of query variants grounded in
       the collection's manifest (``vendor`` / ``products`` / ``description``
       / ``when_to_use``), so a terse / acronym-heavy question retrieves in
       the corpus's own domain terms. The original question is always one of
       the variants.
    2. **Retrieve** — :func:`~meho_backplane.docs_search.retrieve_multi_query`
       runs the shared single-collection retrieval once per variant on the
       same backend (scope gate, entitlement, routing, and forwarded-JWT
       audit still enforced in one place) and RRF-merges the chunks.
    3. **Synthesize** — :func:`~meho_backplane.docs_search.synthesize_docs_answer`
       composes one answer grounded strictly in the merged chunks, answering
       the operator's *original* question.

    Returns ``{answer, citations[]}`` where every citation is a chunk the
    retrieval returned and the model relied on — no claim without a citation.
    The expand step is the **answer-pipeline's** job only: ``search_docs``
    (the raw-chunks tool) is unchanged.

    ``ask_docs`` is **single-collection only** (#1548 decision 2): cross-
    collection synthesis is permanently out of scope. A fan-out attempt —
    ``collections=[…]`` or ``collection="all"`` — is rejected with
    :class:`McpInvalidParamsError` (``-32602``) before any retrieval, so the
    grounded-answer contract never has to reconcile chunks from divergent
    corpora.

    Error arms mirror ``search_docs``'s single-collection path, plus the
    expand + synthesis arms:

    * **Fan-out attempt** (``collections`` / ``collection="all"``) /
      **missing / unknown / not-entitled / disabled ``collection``** — mapped
      to :class:`McpInvalidParamsError` (``-32602``) by
      :func:`_parse_scope_or_invalid_params` /
      :func:`_build_scope_or_invalid_params` /
      :func:`_resolve_collection_or_error` (the MCP analogue of 422 / 403;
      a disabled collection carries ``error.data.reason='collection_disabled'``).
    * **Transiently not-ready collection / corpus unavailable** — not caught;
      they bubble to ``-32603`` (a down / not-serving-yet backend is a
      retryable server fault).
    * **Expand or synthesis LLM leg fails** — not caught; bubbles to
      ``-32603``. ``LlmClientUnavailable`` (no model — both legs reuse the
      same #1386 client), :class:`~meho_backplane.docs_search.DocsQueryExpansionError`
      (expand output unusable), and
      :class:`~meho_backplane.docs_search.DocsSynthesisError` (synthesis broke
      the grounding contract) all fail closed — never an un-expanded /
      ungrounded answer. The expand vs synthesis exception types are distinct
      so a later structured answer-error envelope (#1918) can name the failed
      leg. An empty retrieval short-circuits inside the synthesis helper to a
      deterministic "no grounded answer" *without* a model call.
    """
    # Same canonical-op_id + collection binding as ``search_docs`` —
    # ``ask_docs`` audit rows are filterable by ``op_id="meho.docs.ask"``
    # and ``collection`` across all faces (G4.5-T8 #1549, G4.6-T3 #1552).
    # ``op_class`` stays ``read``; ask is a read-class compose over
    # retrieved chunks.
    structlog.contextvars.bind_contextvars(audit_op_id=_ASK_OP_ID)
    query: str = arguments["query"]
    limit: int = int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT))

    # ``ask_docs`` is single-collection only — reject a fan-out attempt
    # before any retrieval. ``parse_collection_scope`` flags both the
    # ``collections`` list and the ``collection="all"`` sentinel as a
    # fan-out (and a conflicting both-scopes request as -32602).
    parsed = _parse_scope_or_invalid_params("ask_docs", arguments)
    if parsed.is_fanout():
        raise McpInvalidParamsError(
            "ask_docs: cross-collection fan-out (collections / collection='all') "
            "is not supported — ask_docs is single-collection only; use "
            "search_docs for a cross-collection query"
        )

    scope = _build_scope_or_invalid_params("ask_docs", arguments)
    structlog.contextvars.bind_contextvars(audit_collection=scope.collection_key)
    collection = await _resolve_collection_or_error(operator, scope, tool="ask_docs")

    # Corpus-aware expand step (#1916): rewrite the question into a small set
    # of query variants grounded in the collection's manifest, retrieve per
    # variant on the same backend, and RRF-merge the chunks before synthesis.
    # ``expand_docs_query`` fails closed on an unconfigured model
    # (``LlmClientUnavailable``) exactly like synthesis — never an
    # un-expanded, ungrounded answer — and a malformed expansion raises the
    # distinguishable ``DocsQueryExpansionError`` (both bubble to -32603).
    variants = await expand_docs_query(query, collection)
    retrieval = await retrieve_multi_query(
        operator, variants, scope=scope, collection=collection, limit=limit
    )
    # Synthesis grounds on the merged chunks but answers the operator's
    # *original* question (the variants only widened retrieval).
    answer = await synthesize_docs_answer(query, retrieval)
    return {
        "answer": answer.answer,
        "citations": [chunk.model_dump(mode="json") for chunk in answer.citations],
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="ask_docs",
        description=(
            "Answer a vendor-reference question with a SYNTHESIZED, CITED "
            "answer composed over a vendor-document collection (product "
            "manuals, KB articles, design / reference guides) — e.g. 'What "
            "are the NSX 9.0 config maximums for logical switches?'. "
            "This is the answer-shaped sibling of `search_docs`: "
            "`search_docs` returns the raw ranked chunks; `ask_docs` "
            "composes them into one grounded answer and returns the chunks "
            "it cited. "
            "REQUIRES `collection`: it is the hard binary scope (the "
            "question is rejected without it), naming WHICH corpus to "
            "search and gating entitlement — pick it from "
            "`list_doc_collections`. `product` and `version` are OPTIONAL "
            "refinements within that collection, not a ranking hint. "
            "Use this for VENDOR REFERENCE when you want a composed answer "
            "rather than chunks to read yourself; use `search_docs` for the "
            "raw chunks, `search_knowledge` for how THIS team does "
            "something (lab conventions, known-good runbooks, "
            "post-incident learnings), and `search_memory` for "
            "cross-session state. "
            "Returns `{answer, citations[]}`: the answer is grounded "
            "STRICTLY in the collection (no claim without a citation), and "
            "every citation is one of the cited chunks (chunk text, "
            "`source_url`, `chunk_id`, `document_id`). If the collection has "
            "nothing in scope, the answer is 'no grounded answer' — never "
            "a guess. "
            "Limit (chunks retrieved to ground on) defaults to 10; cap is 50."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "Free-form vendor-reference question. Forwarded to "
                        "the collection's backend verbatim for retrieval and "
                        "to the synthesis model; never logged in the clear "
                        "(the audit row stores only its SHA-256 hash)."
                    ),
                },
                "collection": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Collection key to ground the answer on (e.g. "
                        "'vmware'). MANDATORY binary scope — it routes "
                        "retrieval to a backend and gates per-collection "
                        "entitlement; a question without it is rejected with "
                        "INVALID_PARAMS. Pick it from `list_doc_collections`."
                    ),
                },
                "product": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "OPTIONAL vendor-product refinement within the "
                        "collection (e.g. 'nsx', 'vcenter'). Narrows "
                        "retrieval; omit it to ground on the whole collection."
                    ),
                },
                "version": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "OPTIONAL product-version refinement (e.g. '9.0'). "
                        "Narrows retrieval alongside `product`; omit it to "
                        "ground on the whole collection."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                    "default": _DEFAULT_SEARCH_LIMIT,
                    "description": (
                        "Maximum number of ranked cited chunks to retrieve "
                        "and ground the answer on."
                    ),
                },
            },
            "required": ["query", "collection"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_ask_docs_handler,
)
