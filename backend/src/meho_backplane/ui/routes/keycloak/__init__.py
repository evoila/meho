# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak console UI package.

Initiative #1943 (G10.x Keycloak console), Task #1959 (T1). The
read-only realm-administration scaffold: a realm-config card, the
realm's client list + per-client detail, and the realm's client-scope
list -- all rendered from the curated ``keycloak.*`` read ops dispatched
in-process through the operation meta-tools.

The router (:func:`build_keycloak_router`) is a thin BFF over
:func:`meho_backplane.operations.meta_tools.call_operation` called
in-process, mirroring the operations launcher's session-BFF precedent
(:mod:`meho_backplane.ui.routes.operations`) rather than any Bearer-gated
REST surface (there is none for Keycloak -- the CLI verbs ride the
operations dispatcher too).
"""

from __future__ import annotations

from meho_backplane.ui.routes.keycloak.routes import build_keycloak_router

__all__ = ["build_keycloak_router"]
