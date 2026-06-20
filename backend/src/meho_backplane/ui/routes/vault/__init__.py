# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault / secrets console UI routes (read-only KV browser + confirm-gated writes + status view).

Initiative #1942 (G10.18 Vault / secrets console). Task #1956 (T1) ships the
read-only KV browser scaffold (:func:`build_vault_router`); Task #1957 (T2)
adds the confirm-gated KV write verbs in a SEPARATE module
(:func:`build_vault_writes_router` -- put / delete / move); Task #1958 (T3)
adds the read-only status view in its own module
(:func:`build_vault_status_router` -- ``/ui/vault/status`` seal/health/mounts
panel + ``/ui/vault/auth`` auth-methods glance). Splitting the read / write /
status surfaces into separate modules lets them evolve without serial-merge
collisions. Exposes:

* :func:`build_vault_router` -- the T1 ``/ui/vault*`` KV-browser router
  (``/ui/vault`` index + ``/ui/vault/list`` + ``/ui/vault/read`` +
  ``/ui/vault/versions``).
* :func:`build_vault_writes_router` -- the T2 confirm-gated KV write router
  (put / delete / move + their ``…/confirm`` modals).
* :func:`build_vault_status_router` -- the T3 status router
  (``/ui/vault/status`` seal/health/mounts panel + ``/ui/vault/auth``
  auth-methods glance).

All three are included ahead of the stubs aggregate in
:func:`meho_backplane.ui.routes.build_router`.
"""

from __future__ import annotations

from meho_backplane.ui.routes.vault.routes import build_vault_router
from meho_backplane.ui.routes.vault.status import build_vault_status_router
from meho_backplane.ui.routes.vault.writes import build_vault_writes_router

__all__ = [
    "build_vault_router",
    "build_vault_status_router",
    "build_vault_writes_router",
]
