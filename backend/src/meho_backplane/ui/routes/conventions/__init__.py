# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conventions UI routes: kind-tabbed list + budget banner + full-body detail.

Initiative #1838 (G10.12 Conventions console). Task #1895 (T1) ships the
read surface at ``/ui/conventions``: a kind-tabbed (operational /
workflow / reference) summary table, an **always-on preamble token-
budget banner** (estimated/max tokens, dropped slugs shown in red), and
a full-body detail view. Author / edit / delete + history diff land in
T2 (#1838-T2).

Module layout:

* :mod:`~meho_backplane.ui.routes.conventions.routes` -- the request
  handlers + the load-bearing static-before-param route registration.
* :mod:`~meho_backplane.ui.routes.conventions.views` -- the projection
  + render helpers (unit-testable without an HTTP layer); they call the
  shared :class:`~meho_backplane.conventions.service.ConventionsService`
  in-process so the budget arithmetic + ordering match the REST surface
  and T4's MCP preamble packer exactly.
* :mod:`~meho_backplane.ui.routes.conventions.operator` -- the
  ``resolve_read_context`` dependency that synthesises an OPERATOR-tier
  :class:`~meho_backplane.auth.operator.Operator` from the BFF session
  plus a soft ``is_tenant_admin`` UX hint for the T2 write affordances.

The umbrella :func:`build_conventions_router` is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router` so the real
``/ui/conventions`` handler wins the first-match-wins path lookup.
"""

from __future__ import annotations

from meho_backplane.ui.routes.conventions.routes import build_conventions_router

__all__ = ["build_conventions_router"]
