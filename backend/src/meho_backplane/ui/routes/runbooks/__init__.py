# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI surface package (G10.6).

Exports :func:`build_runbooks_router` -- the ``/ui/runbooks*`` router
factory the umbrella :func:`meho_backplane.ui.routes.build_router`
mounts (ahead of the stubs aggregate, first-match-wins). The read surface
(catalog + detail + opacity-floor logic, T1 #1382) lives in
:mod:`meho_backplane.ui.routes.runbooks.routes`; the ``tenant_admin``
authoring editor (T2 #1383) lives in
:mod:`meho_backplane.ui.routes.runbooks.editor` (form logic) +
:mod:`meho_backplane.ui.routes.runbooks.editor_routes` (route wiring),
registered onto the same router by the factory.
"""

from __future__ import annotations

from meho_backplane.ui.routes.runbooks.routes import build_runbooks_router

__all__ = ["build_runbooks_router"]
