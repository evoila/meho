# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://docs/{collection}/{product}/{version}/{chunk_id}`` — docs chunk resource.

G4.6-T3 (#1552), building on G4.5-T4. The fetch-by-citation companion to
the :mod:`search_docs` meta-tool. An agent that kept only a hit's citation
(``collection`` / ``product`` / ``version`` / ``chunk_id``) from an earlier
``search_docs`` call — but not the full chunk text — reads this resource on
a later turn to recover the text without re-running and re-scanning the
whole search.

Gated identically to the tool, per-collection
=============================================

The template carries the same ``required_capability="meho-docs"`` gate as
``search_docs`` (G4.5-T1, #1519): a tenant without the ``meho-docs``
add-on never sees it in ``resources/templates/list`` and a
``resources/read`` on a known URI is rejected with a 403-class error
before the handler runs (enforced at list time by
:func:`~meho_backplane.mcp.registry.all_resource_templates_for` and again
at read time by :func:`~meho_backplane.mcp.handlers.handle_resources_read`).
On top of that visibility gate, the handler enforces the **per-collection**
``meho-docs:<collection>`` entitlement (G4.6-T3 #1552) — so reading a chunk
from a collection the tenant is not entitled to is rejected even when the
add-on is provisioned.

Why the URI carries the scope (and how the fetch works)
=======================================================

The corpus federation client (T2, :mod:`meho_backplane.auth.corpus`)
exposes search-by-query only — there is no fetch-chunk-by-id endpoint to
proxy. So this resource recovers a chunk by **re-issuing a scoped search**
through the same shared
:func:`~meho_backplane.docs_search.search_docs` service the tool uses,
then selecting the hit whose ``chunk_id`` matches the URI. That is why the
``collection`` (plus the optional ``product`` / ``version``) is in the URI:
``collection`` is the mandatory binary scope the re-search needs to route +
entitle, and encoding it lets
:func:`~meho_backplane.docs_search.build_docs_scope` enforce the same
collection-scoped posture the tool enforces (a blank segment can't
physically match the ``[^/]+`` template, so the gate is belt-and-suspenders
here). The ``chunk_id`` is used as the re-search query text — chunk ids are
document-derived tokens, so the backend's own ranking surfaces the matching
chunk near the top of a bounded re-search — and the exact-id match is then
taken from the returned chunks.

Rejection arms
==============

* **Blank scope segment / unknown / not-entitled collection** (all
  ``-32602`` INVALID_PARAMS) —
  :class:`~meho_backplane.docs_search.MissingDocsFilterError`,
  :class:`~meho_backplane.docs_search.UnknownCollectionError`, and
  :class:`~meho_backplane.docs_search.CollectionForbiddenError` map to
  :class:`McpInvalidParamsError`.
* **Chunk not found in the scope** (``-32602``) — the re-search returned no
  chunk whose ``chunk_id`` matches. Collapses to "docs chunk not found"
  without revealing whether the scope is empty or the id is simply absent,
  so the resource is not a probe oracle for the collection contents.

A not-ready collection (:class:`~meho_backplane.docs_search.CollectionNotReadyError`)
or an unavailable backend (:class:`~meho_backplane.auth.corpus.CorpusUnavailable`)
is *not* caught here: it bubbles to the dispatcher's generic catch and
surfaces as ``-32603`` Internal Error — the read was well-formed; the
backend is down / not serving.

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

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.docs_search import (
    CollectionForbiddenError,
    MissingDocsFilterError,
    UnknownCollectionError,
    build_docs_scope,
    resolve_entitled_ready_collection,
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

    Rebuilds the binary collection scope (plus the optional product /
    version refinements) from the URI segments, resolves + entitles +
    readiness-checks the collection, re-issues a scoped search (the
    transport has no fetch-by-id endpoint), and returns the chunk whose
    ``chunk_id`` matches the URI's ``{chunk_id}``. See the module docstring
    for why the fetch is a re-search and for the rejection-arm contract.
    """
    collection_arg = bound["collection"]
    product = bound["product"]
    version = bound["version"]
    chunk_id = bound["chunk_id"]

    try:
        scope = build_docs_scope(collection_arg, product, version)
    except MissingDocsFilterError as exc:
        raise McpInvalidParamsError(f"docs chunk: {exc}") from exc

    structlog.contextvars.bind_contextvars(audit_collection=scope.collection_key)

    # Resolve + entitle + readiness gate (the same shared gate the tools
    # run). Unknown / not-entitled collections map to -32602; a not-ready
    # collection bubbles to -32603 via the dispatcher's generic catch.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            collection = await resolve_entitled_ready_collection(
                session, operator, scope.collection_key
            )
        except UnknownCollectionError as exc:
            raise McpInvalidParamsError(
                f"docs chunk: unknown collection {exc.collection_key!r}",
                data={"known_collections": exc.known_keys},
            ) from exc
        except CollectionForbiddenError as exc:
            raise McpInvalidParamsError(f"docs chunk: {exc}") from exc

    # The transport is search-only, so recover the chunk by re-searching
    # the bound scope and matching on the exact id. The chunk_id doubles
    # as the query text — chunk ids are document-derived, so the backend
    # ranks the matching chunk highly within the bounded window.
    result = await search_docs(
        operator,
        chunk_id,
        scope=scope,
        collection=collection,
        limit=_FETCH_SEARCH_LIMIT,
    )
    for chunk in result.chunks:
        if chunk.chunk_id == chunk_id:
            return chunk.model_dump(mode="json")

    # Not-found collapse: never distinguish "empty scope" from "no such
    # chunk" so the resource can't be used as a collection-contents oracle.
    raise McpInvalidParamsError(
        f"docs chunk not found: collection={collection_arg!r}, "
        f"product={product!r}, version={version!r}, chunk_id={chunk_id!r}",
    )


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://docs/{collection}/{product}/{version}/{chunk_id}",
        name="Vendor-document chunk",
        description=(
            "Full text and citation of one vendor-document chunk, "
            "identified by the collection scope (plus the product / version "
            "refinements) and the chunk id from a `search_docs` hit. Use "
            "after `search_docs` has returned a citation whose chunk text "
            "you no longer have in context — this resource recovers the "
            "chunk's content plus its `source_url` without re-running the "
            "whole search. Returns INVALID_PARAMS for a blank / unknown / "
            "not-entitled collection segment or for a (collection, product, "
            "version, chunk_id) that doesn't resolve to a chunk under the "
            "operator's collection access."
        ),
        mimeType="text/markdown",
        required_role=TenantRole.OPERATOR,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_docs_chunk_handler,
)
