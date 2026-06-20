# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault / secrets console UI routes (read-only KV browser + status view).

Initiative #1942 (G10.18 Vault / secrets console). The read-only KV browser
(T1 #1956) is the scaffold the write (T2 #1957) and status (T3 #1958)
surfaces build on. Exposes:

* :func:`build_vault_router` -- the T1 ``/ui/vault*`` KV-browser router
  (``/ui/vault`` index + ``/ui/vault/list`` + ``/ui/vault/read`` +
  ``/ui/vault/versions``).
* :func:`build_vault_status_router` -- the T3 status router
  (``/ui/vault/status`` seal/health/mounts panel + ``/ui/vault/auth``
  auth-methods glance).

Both are included ahead of the stubs aggregate in
:func:`meho_backplane.ui.routes.build_router`.
"""

from __future__ import annotations

from meho_backplane.ui.routes.vault.routes import build_vault_router
from meho_backplane.ui.routes.vault.status import build_vault_status_router

__all__ = ["build_vault_router", "build_vault_status_router"]
