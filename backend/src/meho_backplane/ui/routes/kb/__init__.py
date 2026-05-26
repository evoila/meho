# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KB UI routes: list/search + entry detail + hover-preview partial + upload.

Initiative #339 (G10.2 Knowledge base UI).

Task #870 (T1) ships the read surface at ``/ui/kb``:

* ``GET /ui/kb`` -- paginated entry list (empty query) or ranked search
  results (HTMX debounced keyup). HTMX partial request returns only
  the ``_results.html`` fragment.
* ``POST /ui/kb/search`` -- the HTMX keyup endpoint.
* ``GET /ui/kb/<slug>`` -- entry detail with server-side Markdown render
  (markdown-it-py GFM + pygments syntax highlight).
* ``GET /ui/kb/<slug>/preview`` -- hover-preview partial with query-term
  highlight markup.

Task #871 (T2) adds the upload surface:

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
is retired by this task).

Markdown editor (T3) will add routes to this package in the next task.
"""

from __future__ import annotations

from meho_backplane.ui.routes.kb.routes import build_kb_router

__all__ = ["build_kb_router"]
