# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault / secrets console UI routes (read-only KV browser).

Initiative #1942 (G10.18 Vault / secrets console), Task #1956 (T1). The
read-only KV browser is the scaffold the write (T2 #1957) and status
(T3 #1958) surfaces build on. Exposes :func:`build_vault_router`, the
``/ui/vault*`` :class:`~fastapi.APIRouter`, included ahead of the stubs
aggregate in :func:`meho_backplane.ui.routes.build_router`.
"""

from __future__ import annotations

from meho_backplane.ui.routes.vault.routes import build_vault_router

__all__ = ["build_vault_router"]
