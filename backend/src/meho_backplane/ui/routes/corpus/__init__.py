# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Docs-corpus UI routes: collection picker + ask-the-corpus + cited chunks.

Initiative #1775 (G10.7 Docs-corpus console surface), Task #1777. The
operator-console face of the federated ``search_docs`` round-trip that
landed as of v0.15.0 (#1732 / #1736): the backend can answer a
collection-scoped vendor-document query, but there was no console surface
to drive it from.

* ``GET /ui/corpus`` -- the docs-corpus page. Renders a collection
  ``<select>`` populated from the same entitled, tenant-scoped catalogue
  ``GET /api/v1/doc_collections`` returns (pre-selected when the operator
  is entitled to exactly one collection), a query input, and an empty
  ``#corpus-results`` region. Mints a CSRF token + sets the ``meho_csrf``
  cookie.
* ``POST /ui/corpus/search`` -- the HTMX search fragment. Reconstructs the
  session operator, calls the in-process
  :func:`~meho_backplane.docs_search.search_docs` service (the same
  primitive the REST route fronts -- no new ``/api/v1`` endpoint), and
  swaps the ``corpus/_results.html`` fragment (one card per cited chunk)
  into ``#corpus-results``. A 403 / 409 / 503 / 422 from the service maps
  to a typed error card; an empty hit list renders a "no results" state.

The router is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router`, and ``POST
/ui/corpus/search`` is registered ahead of any ``/{slug}`` route so the
literal ``search`` segment is never bound as a slug parameter.
"""

from __future__ import annotations

from meho_backplane.ui.routes.corpus.routes import build_corpus_router

__all__ = ["build_corpus_router"]
