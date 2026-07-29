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

* ``GET /ui/corpus/collections`` + ``GET``/``POST``
  ``/ui/corpus/collections/register`` -- the admin Collections lifecycle
  table (rendered as a second tab on ``/ui/corpus``) + the register modal
  (Initiative #1836 / Task #1882). The table lists the tenant's FULL
  registry (NOT entitlement-filtered -- admins manage rows they may not
  hold ``meho-docs:<key>`` for); the register modal drives the in-process
  ``create_doc_collection`` service. Built by
  :func:`~meho_backplane.ui.routes.corpus.collections.build_corpus_collections_router`.

* ``GET /ui/corpus/collections/{collection_key}`` + ``POST
  .../{collection_key}/probe`` + ``GET``/``POST`` ``.../{collection_key}/disable``
  + ``POST .../{collection_key}/enable`` -- the per-collection detail page,
  the HTMX re-probe (readiness-card swap), and confirm-gated enable / disable
  (Initiative #1836 / Task #1883). The detail page renders the full read
  shape (the server-side-only ``backend{type, ref}`` only for a
  ``tenant_admin``); the action verbs drive the in-process ``probe_collection``
  / ``set_collection_enabled`` services. Built by
  :func:`~meho_backplane.ui.routes.corpus.detail.build_corpus_collection_detail_router`.

The router is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router`. Registration order is
**load-bearing**: ``POST /ui/corpus/search`` and the literal
``/ui/corpus/collections/register`` route are registered ahead of the
``/ui/corpus/collections/{collection_key}`` param route (T2 #1883) so the
literal ``search`` / ``register`` segments are never bound as a slug /
``collection_key`` parameter -- first-match-wins.
"""

from __future__ import annotations

from fastapi import APIRouter

from meho_backplane.ui.routes.corpus.chunk_detail import build_corpus_chunk_detail_router
from meho_backplane.ui.routes.corpus.collections import build_corpus_collections_router
from meho_backplane.ui.routes.corpus.detail import build_corpus_collection_detail_router
from meho_backplane.ui.routes.corpus.routes import build_corpus_search_router

__all__ = ["build_corpus_router"]


def build_corpus_router() -> APIRouter:
    """Aggregate the docs-corpus UI routes into one ``/ui/corpus*`` router.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without shared route state -- the chassis convention.
    The collections router (carrying the literal
    ``/ui/corpus/collections/register`` route) is included **before** the
    per-collection detail router (carrying ``/ui/corpus/collections/{collection_key}``)
    so the literal ``register`` segment wins the first-match-wins lookup over
    the bare ``{collection_key}`` detail route (otherwise ``register`` would
    bind as a ``collection_key``) -- the T2 #1883 load-bearing ordering. The
    search router carries ``GET /ui/corpus`` + ``POST /ui/corpus/search``,
    whose paths never collide with the collections paths, so its relative
    order is not load-bearing.
    """
    router = APIRouter()
    router.include_router(build_corpus_collections_router())
    router.include_router(build_corpus_collection_detail_router())
    # The chunk-detail route carries a literal ``chunks`` segment
    # (``/ui/corpus/chunks/{collection_key}/{chunk_id}``, #2462) that never
    # collides with the search / collections paths, so its include order is not
    # load-bearing.
    router.include_router(build_corpus_chunk_detail_router())
    router.include_router(build_corpus_search_router())
    return router
