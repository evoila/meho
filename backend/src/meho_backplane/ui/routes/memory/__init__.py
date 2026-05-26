# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory UI routes: scope-aware list + detail/edit + delete + tag filter.

Initiative #341 (G10.4 Memory UI). Task #877 (G10.4-T1) ships the
read + edit + delete + tag-filter surface at ``/ui/memory``; subsequent
Tasks layer create+promote (T2 #878) and expiry-viz+bulk (T3 #879).

Module layout:

* :mod:`~meho_backplane.ui.routes.memory.routes` -- the request
  handlers. ``GET /ui/memory`` (full page or HTMX card-list fragment),
  ``GET /ui/memory/<scope>/<slug>`` (detail page or HTMX body fragment),
  ``GET /ui/memory/<scope>/<slug>/edit`` (HTMX edit form fragment),
  ``PATCH /ui/memory/<scope>/<slug>`` (HTMX save: replace the body
  fragment), ``DELETE /ui/memory/<scope>/<slug>`` (HTMX delete: re-
  render the list), ``GET /ui/memory/tags`` (HTMX datalist for the
  tag autocomplete).
* :mod:`~meho_backplane.ui.routes.memory.render` -- server-side
  Markdown -> HTML rendering (markdown-it-py commonmark + pygments
  syntax highlight). Mirrors the precedent the KB UI sets in
  :mod:`~meho_backplane.ui.routes.kb.render` (G10.2-T1 #870); the two
  modules will dedupe once both PRs land on ``main``.
* :mod:`~meho_backplane.ui.routes.memory.operator` -- the
  ``resolve_ui_operator`` FastAPI dependency that lifts the full
  :class:`~meho_backplane.auth.operator.Operator` (carrying
  :attr:`tenant_role`) from the BFF session by re-verifying the
  stored access token. The chassis :class:`UISessionContext` only
  carries ``operator_sub`` + ``tenant_id``; the memory RBAC matrix
  needs the role to gate edit-in-place + tenant-scoped writes.

The umbrella :func:`build_router` is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router` so the real
``/ui/memory`` handler wins the first-match-wins path lookup. The
``memory`` stub is retired by this task.
"""

from __future__ import annotations

from meho_backplane.ui.routes.memory.routes import build_memory_router

__all__ = ["build_memory_router"]
