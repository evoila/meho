# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-console UI chassis -- server-rendered HTMX + Jinja2 + Tailwind 4.

Initiative #337 (G10.0 Frontend chassis) lands the operator-facing web UI
on top of the backplane FastAPI process. The console is served at ``/ui/*``
from the same uvicorn worker that already serves ``/api/v1/*`` and
``/mcp`` -- one process, one TLS termination, one ingress, one auth
boundary (per v0.2-decisions.md #10).

This Task (#863, G10.0-T2) ships the **chassis** only: the module layout
that T3 / T4 / T5 fill in. No FastAPI routes are wired yet (T5 #866 mounts
``StaticFiles`` + the ``/ui`` router). No auth flow yet (T4 #865 ships
``login`` / ``callback`` / ``logout``). No session storage yet (T3 #864
ships the ``web_session`` ORM + encrypted-token custody).

Module map:

* :mod:`meho_backplane.ui.paths` -- resolves the ``templates/``,
  ``static/src/``, and ``static/dist/`` directories at runtime via
  ``importlib.resources`` so the wheel/image deploy and the local
  ``uvicorn --reload`` source-tree deploy both work without a path
  hardcode.
* :mod:`meho_backplane.ui.templating` -- :class:`jinja2.Environment`
  factory with autoescape + StrictUndefined so a typo in a template
  variable name fails the request instead of silently rendering empty.
* :mod:`meho_backplane.ui.routes` -- (T5 #866) FastAPI APIRouter
  package; ``__init__.py`` is a stub for this Task.
* :mod:`meho_backplane.ui.auth` -- (T4 #865) BFF session middleware +
  Authorization Code + PKCE flow; ``__init__.py`` is a stub for this
  Task.

Stack (locked, v0.2-decisions.md #9):

* **HTMX 2.0.9** for partial swaps.
* **Jinja2 >= 3.1.6** (already a backplane dep) for HTML templating.
* **Tailwind CSS 4.3.0** -- standalone CLI binary (pinned SHA256 in
  ``backend/Dockerfile``) builds ``static/dist/tailwind.css`` at
  image-build time. No ``node_modules`` enters the image.
* **DaisyUI 5.5.20** -- Tailwind-4 plugin, loaded via ``@plugin`` in
  ``static/src/styles.css``. Vendored at
  ``static/src/vendor/daisyui.js``.
* **Alpine.js 3.15.12** for the modest interactive surfaces (sidebar
  collapse, user-menu open/close) HTMX alone can't carry.
* **Cytoscape.js 3.33.4** for the G10.5 topology graph viz island
  (vendored now; consumed by the surface Initiative).

The Tailwind build step lives in ``backend/Dockerfile``. Local dev runs
the same CLI in watch mode (``tailwindcss --watch``) alongside
``uvicorn --reload``; see ``docs/codebase/ui.md``.
"""
