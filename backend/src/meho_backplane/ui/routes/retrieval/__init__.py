# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retrieval-diagnostics UI surface package.

Initiative #1840 (G10.14 Retrieval diagnostics & quality console), Task #1888.

The anchor Task stands up the ``/ui/retrieval`` page scaffold (the sidebar
entry, the tab strip, the page chrome) + its default Diagnostics tab. Sibling
Tasks T2 (Usage / Eval tabs) and T3 (Retire-checklist tab) nest into the same
page as additional client-side tab panels.

Exposes :func:`~meho_backplane.ui.routes.retrieval.routes.build_retrieval_router`
-- the ``GET /ui/retrieval`` + ``POST /ui/retrieval/diagnostics`` factory -- so
:func:`meho_backplane.ui.routes.build_router` can include it ahead of the stubs
router. The factory pattern (not a module-level constant) mirrors the corpus /
kb chassis convention so a test app can build parallel routers without sharing
route state.
"""

from __future__ import annotations

from meho_backplane.ui.routes.retrieval.routes import build_retrieval_router

__all__ = ["build_retrieval_router"]
