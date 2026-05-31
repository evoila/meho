# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runbooks UI read surface package (G10.6-T1, #1382).

Exports :func:`build_runbooks_router` -- the ``/ui/runbooks*`` router
factory the umbrella :func:`meho_backplane.ui.routes.build_router`
mounts (ahead of the stubs aggregate, first-match-wins). The catalog +
detail handlers and the opacity-floor logic live in
:mod:`meho_backplane.ui.routes.runbooks.routes`.
"""

from __future__ import annotations

from meho_backplane.ui.routes.runbooks.routes import build_runbooks_router

__all__ = ["build_runbooks_router"]
