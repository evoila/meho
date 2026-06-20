# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault / secrets console UI routes (read-only KV browser + confirm-gated writes).

Initiative #1942 (G10.18 Vault / secrets console). Task #1956 (T1) ships the
read-only KV browser scaffold (:func:`build_vault_router`); Task #1957 (T2)
adds the confirm-gated KV write verbs in a SEPARATE module
(:func:`build_vault_writes_router` -- put / delete / move) so the read and
write surfaces (and the parallel T3 status view #1958) evolve without
serial-merge collisions. Both routers are included ahead of the stubs
aggregate in :func:`meho_backplane.ui.routes.build_router`.
"""

from __future__ import annotations

from meho_backplane.ui.routes.vault.routes import build_vault_router
from meho_backplane.ui.routes.vault.writes import build_vault_writes_router

__all__ = ["build_vault_router", "build_vault_writes_router"]
