# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KB UI routes: list/search + entry detail + hover-preview + editor + upload.

Initiative #339 (G10.2 Knowledge base UI). Tasks #870 (T1) + #872 (T3)
+ #871 (T2).

T1 (read surface):

* ``GET /ui/kb`` -- paginated entry list (empty query) or ranked search
  results (HTMX debounced keyup). HTMX partial request returns only
  the ``_results.html`` fragment.
* ``POST /ui/kb/search`` -- the HTMX keyup endpoint.
* ``GET /ui/kb/<slug>`` -- entry detail with server-side Markdown render
  (markdown-it-py GFM + pygments syntax highlight).
* ``GET /ui/kb/<slug>/preview`` -- hover-preview partial with query-term
  highlight markup.

T3 (editor modal + mobile reflow):

* ``POST /ui/kb/editor-preview`` -- HTMX editor live-preview partial.
  Accepts ``body`` form field, renders via ``render_markdown``, returns
  the ``_editor_preview.html`` fragment. Any authenticated operator can
  call this (read-only Markdown transform).
* ``POST /ui/kb/new`` -- editor save. Requires ``tenant_admin`` role
  (enforced by re-verifying the session access token through
  :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`). Saves via
  :meth:`~meho_backplane.kb.KbService.create_entry`, returns
  ``HX-Redirect`` to the new entry's detail page on success or
  re-renders the modal with an inline error message on failure.

T2 (upload surface):

* ``GET /ui/kb/upload`` -- upload page with Alpine.js drag-and-drop
  component. ``tenant_admin`` role required.
* ``POST /ui/kb/upload`` -- single-file upload endpoint. Returns the
  ``kb/_upload_progress.html`` HTMX partial.
* ``POST /ui/kb/upload/bulk`` -- bulk upload endpoint. Returns the
  same partial with per-file progress rows.

The router is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router` so the real ``/ui/kb``
handler wins the first-match-wins path lookup (the ``knowledge`` stub
is retired by T1).
"""

from __future__ import annotations

from meho_backplane.ui.routes.kb.routes import build_kb_router

__all__ = ["build_kb_router"]
