# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.proxmox — ProxmoxConnector package (#2238).

Importing this package registers :class:`ProxmoxConnector` against the v2
connector registry under the natural key
``(product="proxmox", version="8.x", impl_id="proxmox-api")`` **and** the
``(product="proxmox", version="", impl_id="")`` wildcard fallback — dual
registration from day one per G0.15-T6 (#1215).

Two-phase registration (same shape as pfsense / bind9 / argocd):

* **Synchronous (import time)** — the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` so the
  lookup tables are populated before the lifespan begins and a probe firing
  during startup sees a fully-populated registry.
* **Asynchronous (lifespan startup)** —
  :func:`register_proxmox_typed_operations` (queued via
  :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`)
  delegates to :meth:`ProxmoxConnector.register_operations`, which upserts the
  read ops (``proxmox.about`` / ``proxmox.api.get`` / ``proxmox.task.status``)
  and the approval-gated write op (``proxmox.api.write``,
  ``requires_approval=True``).

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is intentionally **not** called: Proxmox has no v1 chassis history, and
a v1 entry would land as ``("proxmox", "", "")`` and confuse the resolver
tie-break ladder. Only the v2 triple (+ wildcard) advertises this class — the
argocd / bind9 / pfsense decision.
"""

from meho_backplane.connectors.proxmox.connector import ProxmoxConnector
from meho_backplane.connectors.proxmox.ops import (
    PROXMOX_READ_OPS,
    PROXMOX_WHEN_TO_USE_BY_GROUP,
    ProxmoxOp,
)
from meho_backplane.connectors.proxmox.ops_write import (
    PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP,
    PROXMOX_WRITE_OPS,
)
from meho_backplane.connectors.proxmox.session import (
    ProxmoxCredentials,
    ProxmoxCredentialsLoader,
    ProxmoxTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_proxmox_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``ProxmoxConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault #390)
    is a module-level ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    The ``embedding_service`` keyword-only parameter is accepted-and-discarded
    (the runner passes it to every registrar) because
    :meth:`ProxmoxConnector.register_operations` resolves the embedding
    service via ``register_typed_operation``'s process-wide singleton
    fallback — the bind9 / argocd sibling shape.
    """
    del embedding_service  # accepted for runner-compatibility; see docstring
    await ProxmoxConnector.register_operations()


__all__ = [
    "PROXMOX_READ_OPS",
    "PROXMOX_WHEN_TO_USE_BY_GROUP",
    "PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP",
    "PROXMOX_WRITE_OPS",
    "ProxmoxConnector",
    "ProxmoxCredentials",
    "ProxmoxCredentialsLoader",
    "ProxmoxOp",
    "ProxmoxTargetLike",
    "load_credentials_from_vault",
    "register_proxmox_typed_operations",
]


# v2 entry — the canonical resolver key. The versioned triple always wins the
# resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="proxmox",
    version="8.x",
    impl_id="proxmox-api",
    cls=ProxmoxConnector,
)

# G0.15-T6 (#1215) wildcard fallback — a target with ``version=None`` (fresh,
# unfingerprinted) resolves to this connector through the resolver's
# ``versioned_over_wildcard`` step rather than 501-ing with ``no_connector``.
register_connector_v2(
    product="proxmox",
    version="",
    impl_id="",
    cls=ProxmoxConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
register_typed_op_registrar(register_proxmox_typed_operations)
