# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Internal cited-source detail view for a ``meho://`` citation (#2462).

Initiative #2495 (G0.33 operator-console hardening), Task #2462.

The ``/ui/corpus`` citation cards (#1919) render two kinds of source
affordance, keyed on the chunk's normalized ``source_url``:

* a chunk whose source resolves to a **canonical public URL** (a Broadcom
  KB article, an already-``https`` link) gets an **outbound** anchor that
  opens the source in a new tab — unchanged by design (#1919 AC 2);
* a chunk whose source has **no derivable public URL** carries an opaque
  ``meho://docs/<collection>/<chunk_id>`` reference (#132). Before this
  task that citation was plain, non-clickable text — a dead end: an
  operator reading a grounded Ask answer could open every *external*
  citation but was stuck on an internal-ref one.

This module adds the internal landing that closes the affordance gap:

* ``GET /ui/corpus/chunks/{collection_key}/{chunk_id}`` — a readable
  detail view of one cited source. It resolves the ``collection_key``
  tenant-first (404 on an unknown / cross-tenant key), enforces the same
  per-collection entitlement the search path enforces (403 naming the
  missing ``meho-docs:<key>`` capability when the identity is not
  entitled), and renders the chunk's identity + provenance: which
  collection it came from (linked to the collection detail page), its
  stable ``meho://docs/<collection>/<chunk_id>`` reference, and the
  collection's vendor / products.

Why identity + provenance, not the chunk body
---------------------------------------------

The retrieved chunk's *content* is already rendered inline on the citation
card. Re-fetching a single chunk by id for a fuller render is not possible
today: the backend-agnostic
:class:`~meho_backplane.docs_search.backends.base.SearchBackend` interface
exposes ``search`` + ``probe`` only — there is no fetch-by-id seam, and
adding one is a cross-adapter capability out of scope for this task. So the
detail view is the citation's **entitlement-checked, shareable permalink**:
it confirms *where a grounded answer's claim came from* and links onward to
the collection, rather than mint a link that 404s. A per-chunk body
re-fetch is a forward arm for when the backend interface grows a get-by-id
method (mirrors the ``stored_object`` proxy forward arm in
:mod:`~meho_backplane.docs_search.citation_links`).

Route ordering
--------------

The route carries a literal ``chunks`` segment
(``/ui/corpus/chunks/{collection_key}/{chunk_id}``) that never collides
with the sibling ``/ui/corpus`` / ``/ui/corpus/search`` /
``/ui/corpus/collections/*`` paths, so its include order in
:func:`~meho_backplane.ui.routes.corpus.build_corpus_router` is not
load-bearing. ``chunk_id`` is a ``:path`` converter so an id carrying a
``/`` (the ``meho://`` ref's leaf can, in principle) is captured whole.

The view is read-only (a ``GET``), so — unlike the collection detail's
probe / enable / disable verbs — it mints no CSRF token and sets no cookie.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_session
from meho_backplane.docs_collections import (
    project_doc_collection_to_summary,
    resolve_doc_collection,
)
from meho_backplane.docs_search import collection_capability_key
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.corpus.routes import _resolve_operator, internal_chunk_ref
from meho_backplane.ui.templating import get_templates

__all__ = ["build_corpus_chunk_detail_router"]

_log = structlog.get_logger(__name__)

#: Collection keys cap at 128 chars in the schema (``DocCollectionCreate``);
#: a longer segment cannot name a real row, so reject it with the same 404 the
#: resolver would raise — saving the query on the fuzzer-spam vector (the
#: collection-detail idiom, :mod:`~meho_backplane.ui.routes.corpus.detail`).
_COLLECTION_KEY_MAX = 128

#: A defensive upper bound on the chunk-id path segment. Corpus chunk ids are
#: short opaque tokens; an over-long value cannot name a real citation, so it
#: is rejected as a 404 rather than forwarded.
_CHUNK_ID_MAX = 512

#: Module-level ``Depends`` closures — ruff B008 idiom matching the sibling
#: corpus routes (no function calls in default argument positions).
_require_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)


async def _render_chunk_detail(
    request: Request,
    *,
    collection_key: str,
    chunk_id: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Render the internal cited-source detail page for one ``meho://`` chunk.

    Resolves the collection tenant-first (404 unknown / cross-tenant),
    entitlement-gates it against the reconstructed operator (403 naming the
    missing capability), and renders the chunk's identity + provenance. The
    chunk body is not re-fetched (no backend get-by-id seam); the view is the
    citation's entitlement-checked permalink, linking onward to the collection.
    """
    collection_key = collection_key.strip()
    chunk_id = chunk_id.strip()
    if (
        not collection_key
        or not chunk_id
        or len(collection_key) > _COLLECTION_KEY_MAX
        or len(chunk_id) > _CHUNK_ID_MAX
    ):
        raise HTTPException(status_code=404, detail="cited source not found")

    operator = await _resolve_operator(session_ctx)
    # 404 on an unknown / cross-tenant key (``DocCollectionNotFoundError`` is an
    # ``HTTPException`` with status 404).
    collection = await resolve_doc_collection(db_session, collection_key, operator.tenant_id)

    # Entitlement gate: the same per-collection ``meho-docs:<key>`` capability
    # the search / ask paths enforce. An identity that cannot search the
    # collection cannot inspect its citations either — a 403 naming the missing
    # capability + the identity it checked (the T2 #1802 diagnostic shape).
    required_capability = collection_capability_key(collection_key)
    if required_capability not in operator.capabilities:
        _log.info(
            "ui_corpus_chunk_detail_forbidden",
            collection_key=collection_key,
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            required_capability=required_capability,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "not_entitled",
                "collection": collection_key,
                "required_capability": required_capability,
                "operator_sub": operator.sub,
                "tenant_id": str(operator.tenant_id),
                "message": (
                    f"Not entitled to inspect citations for collection "
                    f"{collection_key!r}: the identity {operator.sub!r} in tenant "
                    f"{operator.tenant_id} is missing the {required_capability!r} "
                    "capability."
                ),
            },
        )

    context: dict[str, Any] = {
        "active_surface": "corpus",
        "page_title": f"Cited source · {collection_key}",
        "collection": project_doc_collection_to_summary(collection).model_dump(),
        "chunk_id": chunk_id,
        # The stable backend-agnostic reference the citation carried (#132);
        # displayed so the operator can copy it for support / cross-reference.
        "chunk_ref": internal_chunk_ref(collection_key, chunk_id),
    }
    return get_templates().TemplateResponse(request, "corpus/chunk_detail.html", context)


def build_corpus_chunk_detail_router() -> APIRouter:
    """Construct the ``GET /ui/corpus/chunks/{collection_key}/{chunk_id}`` router.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without shared route state — the corpus / connectors
    router convention.
    """
    router = APIRouter(tags=["ui-corpus"])

    async def _chunk_detail_handler(
        request: Request,
        collection_key: str,
        chunk_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/corpus/chunks/{collection_key}/{chunk_id}`` — cited-source view."""
        return await _render_chunk_detail(
            request,
            collection_key=collection_key,
            chunk_id=chunk_id,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    router.add_api_route(
        "/ui/corpus/chunks/{collection_key}/{chunk_id:path}",
        _chunk_detail_handler,
        methods=["GET"],
        name="ui_corpus_chunk_detail",
        response_class=HTMLResponse,
    )
    return router
