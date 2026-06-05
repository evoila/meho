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
``audit_log`` row per ``tools/call`` with ``op_id="search_docs"`` and the
``op_class="read"`` declared below, and hashes the raw arguments into
``params_hash`` — so the query is recorded only as a hash, never in the
clear, matching the route's ``meho.docs.search`` privacy posture. (The
route's ``meho.docs.search`` op_id is an HTTP-path concern bound by the
chassis middleware; the MCP audit op_id is the tool name verbatim so
``classify_op`` resolves the ``.search`` → ``read`` sensitivity class
correctly.) Tenant scoping rides the operator's forwarded JWT: the
service hands ``operator.raw_jwt`` to the corpus, which authenticates and
audits the call as the operator; there is no tool argument that names a
tenant.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_search import (
    MissingDocsFilterError,
    build_docs_scope,
    search_docs,
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


async def _search_docs_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Federate a vendor-document query through the shared docs-search service.

    Validates the mandatory product+version binary scope, then delegates
    to :func:`~meho_backplane.docs_search.search_docs` — the same service
    the REST route fronts — forwarding the operator's JWT so the corpus
    authenticates and audits the call as the operator.

    Two error arms:

    * **Missing/blank product or version** —
      :class:`~meho_backplane.docs_search.MissingDocsFilterError` from
      :func:`build_docs_scope` (when REQUIRE_FILTERS is on) is re-raised
      as :class:`McpInvalidParamsError` so the dispatcher emits the
      spec-correct ``-32602`` (the MCP analogue of the route's 422). The
      inputSchema's ``required`` list catches this first for a
      schema-validating client; this arm covers the gate-off → gate-on
      flip and non-validating clients.
    * **Corpus unavailable** — the typed
      :class:`~meho_backplane.auth.corpus.CorpusUnavailable` is *not*
      caught here. It bubbles to the dispatcher's generic catch and
      surfaces as ``-32603`` Internal Error (the MCP analogue of the
      route's 503): a well-formed request against a down upstream is a
      server-side fault, not invalid params.
    """
    query: str = arguments["query"]
    product: str = arguments["product"]
    version: str = arguments["version"]
    limit: int = int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT))

    try:
        scope = build_docs_scope(product, version)
    except MissingDocsFilterError as exc:
        raise McpInvalidParamsError(f"search_docs: {exc}") from exc

    result = await search_docs(operator, query, scope=scope, limit=limit)
    return {
        "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
    }


register_mcp_tool(
    definition=ToolDefinition(
        name="search_docs",
        description=(
            "Search the vendor-document corpus (product manuals, KB "
            "articles, design / reference guides) for an authoritative "
            "vendor fact — e.g. 'NSX config maximums for 9.0' or "
            "'vCenter 8.0 supported snapshot depth'. "
            "REQUIRES `product` AND `version`: they are a hard binary "
            "scope (the query is rejected without both), NOT a ranking "
            "hint. "
            "Use this for VENDOR REFERENCE — what the documentation says. "
            "Use `search_knowledge` instead for how THIS team does "
            "something (lab conventions, known-good runbooks, "
            "post-incident learnings), and `search_memory` for "
            "cross-session state (what you or the operator established "
            "earlier in this or a prior session). "
            "Returns ranked cited chunks: each carries the chunk text, a "
            "`source_url` citation, a `chunk_id`, and a `document_id`. "
            "For the full text of a hit on a later turn (when you kept "
            "only the citation), read `meho://docs/{product}/{version}/"
            "{chunk_id}` via `resources/read`. "
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
                        "Free-form vendor-reference query. Forwarded to the "
                        "corpus verbatim; never logged in the clear (the "
                        "audit row stores only its SHA-256 hash)."
                    ),
                },
                "product": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Vendor product to scope to (e.g. 'nsx', 'vcenter'). "
                        "MANDATORY binary scope, not a hint — a query "
                        "without it is rejected with INVALID_PARAMS while "
                        "the REQUIRE_FILTERS posture is on."
                    ),
                },
                "version": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Product version to scope to (e.g. '9.0'). "
                        "MANDATORY binary scope alongside `product` — "
                        "both are required to bound the search to one "
                        "product / version slice."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                    "default": _DEFAULT_SEARCH_LIMIT,
                    "description": "Maximum number of ranked cited chunks to return.",
                },
            },
            "required": ["query", "product", "version"],
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

    The synthesis fast-follow to ``search_docs``: it runs the **same**
    shared retrieval (so the REQUIRE_FILTERS gate, corpus federation, and
    forwarded-JWT audit are enforced in exactly one place), then composes a
    single answer grounded strictly in the retrieved chunks via
    :func:`~meho_backplane.docs_search.synthesize_docs_answer`. Returns
    ``{answer, citations[]}`` where every citation is a chunk the retrieval
    returned and the model relied on — no claim without a citation.

    Error arms mirror ``search_docs`` exactly, plus the synthesis arm:

    * **Missing/blank product or version** —
      :class:`~meho_backplane.docs_search.MissingDocsFilterError` from
      :func:`build_docs_scope` re-raised as :class:`McpInvalidParamsError`
      (``-32602``, the MCP analogue of the route's 422). The inputSchema's
      ``required`` list catches this first for a schema-validating client.
    * **Corpus unavailable** —
      :class:`~meho_backplane.auth.corpus.CorpusUnavailable` is *not*
      caught; it bubbles to ``-32603`` (a down upstream is a server fault).
    * **Synthesis model unconfigured / unreachable** —
      :class:`~meho_backplane.operations.ingest.LlmClientUnavailable` (and
      :class:`~meho_backplane.docs_search.DocsSynthesisError` for a model
      that ran but broke the grounding contract) are likewise *not* caught;
      they bubble to ``-32603``. We never degrade to an ungrounded answer —
      a fail-closed 503-analogue is the correct posture for a grounded-
      reference add-on. An empty retrieval is handled inside the synthesis
      helper, which returns a deterministic "no grounded answer" *without*
      calling the model.
    """
    query: str = arguments["query"]
    product: str = arguments["product"]
    version: str = arguments["version"]
    limit: int = int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT))

    try:
        scope = build_docs_scope(product, version)
    except MissingDocsFilterError as exc:
        raise McpInvalidParamsError(f"ask_docs: {exc}") from exc

    retrieval = await search_docs(operator, query, scope=scope, limit=limit)
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
            "answer composed over the vendor-document corpus (product "
            "manuals, KB articles, design / reference guides) — e.g. 'What "
            "are the NSX 9.0 config maximums for logical switches?'. "
            "This is the answer-shaped sibling of `search_docs`: "
            "`search_docs` returns the raw ranked chunks; `ask_docs` "
            "composes them into one grounded answer and returns the chunks "
            "it cited. "
            "REQUIRES `product` AND `version`: they are a hard binary "
            "scope (the question is rejected without both), NOT a ranking "
            "hint. "
            "Use this for VENDOR REFERENCE when you want a composed answer "
            "rather than chunks to read yourself; use `search_docs` for the "
            "raw chunks, `search_knowledge` for how THIS team does "
            "something (lab conventions, known-good runbooks, "
            "post-incident learnings), and `search_memory` for "
            "cross-session state. "
            "Returns `{answer, citations[]}`: the answer is grounded "
            "STRICTLY in the corpus (no claim without a citation), and "
            "every citation is one of the cited chunks (chunk text, "
            "`source_url`, `chunk_id`, `document_id`). If the corpus has "
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
                        "the corpus verbatim for retrieval and to the "
                        "synthesis model; never logged in the clear (the "
                        "audit row stores only its SHA-256 hash)."
                    ),
                },
                "product": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Vendor product to scope to (e.g. 'nsx', 'vcenter'). "
                        "MANDATORY binary scope, not a hint — a question "
                        "without it is rejected with INVALID_PARAMS while "
                        "the REQUIRE_FILTERS posture is on."
                    ),
                },
                "version": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Product version to scope to (e.g. '9.0'). "
                        "MANDATORY binary scope alongside `product` — "
                        "both are required to bound the search to one "
                        "product / version slice."
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
            "required": ["query", "product", "version"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_ask_docs_handler,
)
