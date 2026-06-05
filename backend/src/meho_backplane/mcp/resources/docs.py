# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://docs/{product}/{version}/{chunk_id}`` — docs chunk resource (G4.5-T4).

The fetch-by-citation companion to the :mod:`search_docs` meta-tool. An
agent that kept only a hit's citation (``product`` / ``version`` /
``chunk_id``) from an earlier ``search_docs`` call — but not the full
chunk text — reads this resource on a later turn to recover the text
without re-running and re-scanning the whole search.

Gated identically to the tool
=============================

The template carries the same ``required_capability="meho-docs"`` gate as
``search_docs`` (G4.5-T1, #1519): a tenant without the ``meho-docs``
add-on never sees it in ``resources/templates/list`` and a
``resources/read`` on a known URI is rejected with a 403-class error
before the handler runs. The gate is enforced at list time
(:func:`~meho_backplane.mcp.registry.all_resource_templates_for`) and
again at read time
(:func:`~meho_backplane.mcp.handlers.handle_resources_read`).

Why the URI carries the scope (and how the fetch works)
=======================================================

The corpus federation client (T2, :mod:`meho_backplane.auth.corpus`)
exposes search-by-query only — there is no fetch-chunk-by-id endpoint to
proxy. So this resource recovers a chunk by **re-issuing a scoped corpus
search** through the same shared
:func:`~meho_backplane.docs_search.search_docs` service the tool uses,
then selecting the hit whose ``chunk_id`` matches the URI. That is why the
``product`` and ``version`` are in the URI rather than just a bare
``chunk_id``: they are the mandatory binary scope the re-search needs, and
encoding them lets :func:`~meho_backplane.docs_search.build_docs_scope`
enforce the same REQUIRE_FILTERS posture the tool enforces (a blank
segment can't physically match the ``[^/]+`` template, so the gate is
belt-and-suspenders here). The ``chunk_id`` is used as the re-search query
text — chunk ids are document-derived tokens, so the corpus's own ranking
surfaces the matching chunk near the top of a bounded re-search — and the
exact-id match is then taken from the returned chunks.

Rejection arms (all ``-32602`` INVALID_PARAMS)
==============================================

* **Blank scope segment** —
  :class:`~meho_backplane.docs_search.MissingDocsFilterError` from
  :func:`build_docs_scope` (only reachable with the gate on and a
  segment that survived the template but is blank-after-strip) maps to
  :class:`McpInvalidParamsError`.
* **Chunk not found in the scope** — the re-search returned no chunk
  whose ``chunk_id`` matches. Collapses to "docs chunk not found"
  without revealing whether the scope is empty or the id is simply
  absent, so the resource is not a probe oracle for the corpus contents.

A corpus that is unavailable raises the typed
:class:`~meho_backplane.auth.corpus.CorpusUnavailable`, which is *not*
caught here: it bubbles to the dispatcher's generic catch and surfaces as
``-32603`` Internal Error — the read was well-formed; the upstream is
down.

Response shape
==============

``resources/read`` returns a ``contents[]`` array; the dispatcher wraps
this handler's return value in one text block whose ``text`` is the
JSON-serialised :class:`~meho_backplane.docs_search.DocsChunk`
(``chunk_id`` / ``document_id`` / ``content`` / ``source_url`` /
``score``). ``mimeType`` is ``text/markdown`` — vendor-doc chunk content
is prose, often Markdown-shaped.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_search import (
    MissingDocsFilterError,
    build_docs_scope,
    search_docs,
)
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError

#: Same capability gate as the ``search_docs`` tool — the resource is the
#: tool's companion and must not be reachable when the tool is hidden.
_DOCS_CAPABILITY: Final[str] = "meho-docs"

#: How many chunks to request when re-searching for the cited chunk. A
#: small bound keeps the corpus round-trip cheap; the exact ``chunk_id``
#: match is taken from whatever the corpus returns within this window.
_FETCH_SEARCH_LIMIT: Final[int] = 50


async def _docs_chunk_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Return the cited :class:`~meho_backplane.docs_search.DocsChunk` for the URI.

    Rebuilds the binary product+version scope from the URI segments,
    re-issues a scoped corpus search (the transport has no
    fetch-by-id endpoint), and returns the chunk whose ``chunk_id``
    matches the URI's ``{chunk_id}``. See the module docstring for why
    the fetch is a re-search and for the rejection-arm contract.
    """
    product = bound["product"]
    version = bound["version"]
    chunk_id = bound["chunk_id"]

    try:
        scope = build_docs_scope(product, version)
    except MissingDocsFilterError as exc:
        raise McpInvalidParamsError(f"docs chunk: {exc}") from exc

    # The transport is search-only, so recover the chunk by re-searching
    # the bound scope and matching on the exact id. The chunk_id doubles
    # as the query text — chunk ids are document-derived, so the corpus
    # ranks the matching chunk highly within the bounded window.
    result = await search_docs(
        operator,
        chunk_id,
        scope=scope,
        limit=_FETCH_SEARCH_LIMIT,
    )
    for chunk in result.chunks:
        if chunk.chunk_id == chunk_id:
            return chunk.model_dump(mode="json")

    # Not-found collapse: never distinguish "empty scope" from "no such
    # chunk" so the resource can't be used as a corpus-contents oracle.
    raise McpInvalidParamsError(
        f"docs chunk not found: product={product!r}, version={version!r}, chunk_id={chunk_id!r}",
    )


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://docs/{product}/{version}/{chunk_id}",
        name="Vendor-document chunk",
        description=(
            "Full text and citation of one vendor-document chunk, "
            "identified by the product + version scope and the chunk id "
            "from a `search_docs` hit. Use after `search_docs` has "
            "returned a citation whose chunk text you no longer have in "
            "context — this resource recovers the chunk's content plus "
            "its `source_url` without re-running the whole search. "
            "Returns INVALID_PARAMS for a blank scope segment or for a "
            "(product, version, chunk_id) that doesn't resolve to a "
            "chunk under the operator's federated corpus access."
        ),
        mimeType="text/markdown",
        required_role=TenantRole.OPERATOR,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_docs_chunk_handler,
)
