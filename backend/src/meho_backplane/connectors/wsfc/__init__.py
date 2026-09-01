# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.wsfc -- WsfcConnector package.

Importing the package registers :class:`WsfcConnector` against the v2 connector
registry under the natural key ``(product="wsfc", version="2022.x",
impl_id="wsfc-ssh")``, and queues the connector's typed-op upserts onto the
lifespan-driven registrar list so ``endpoint_descriptor`` rows land before the
first dispatch. The package is discovered automatically by
:func:`~meho_backplane.connectors.registry._eager_import_connectors` (which
imports every ``connectors/<product>/`` subpackage in name-sorted order) --
there is no central connector list to edit; self-registration at import time is
the whole wiring.

Two-phase registration (same shape as
:mod:`meho_backplane.connectors.winsrv.__init__`):

* **Synchronous (import time)** -- the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` inside this
  module, so a probe firing during startup sees a fully-populated registry.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_wsfc_typed_operations`, which delegates to
  :meth:`WsfcConnector.register_operations`.

The ``(product, version, impl_id)`` triple is chosen so
``connector_id="wsfc-ssh-2022.x"`` round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id` (``product`` =
the first hyphen-segment of ``impl_id`` = ``"wsfc"``); a hyphen/underscore in
the product token would break that round-trip and the registry's
``_assert_product_impl_id_round_trips`` guard would crash
``_eager_import_connectors`` at boot.

Like winsrv / rke2, this connector has no v1 chassis history, so the v1
``register_connector`` write is intentionally omitted -- only the v2 triple
advertises this class.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.wsfc.connector import WsfcConnector
from meho_backplane.connectors.wsfc.ops import WSFC_OPS, WsfcOp
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_wsfc_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``WsfcConnector.register_operations``.

    The canonical typed-op registration pattern is a module-level ``async def
    register_xxx_typed_operations`` queued onto
    :func:`run_typed_op_registrars` via :func:`register_typed_op_registrar`. The
    ``embedding_service`` keyword-only parameter mirrors the winsrv / rke2
    sibling contract -- :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` to every registrar, so each registrar **must**
    accept the kwarg or the lifespan crashes with :class:`TypeError`. The
    wrapper accepts-and-discards it because
    :meth:`WsfcConnector.register_operations` resolves the embedding service via
    ``register_typed_operation``'s process-wide singleton fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await WsfcConnector.register_operations()


__all__ = [
    "WSFC_OPS",
    "WsfcConnector",
    "WsfcOp",
    "register_wsfc_typed_operations",
]


# v2 entry -- the canonical resolver key.
register_connector_v2(
    product="wsfc",
    version="2022.x",
    impl_id="wsfc-ssh",
    cls=WsfcConnector,
)

# Wildcard fallback -- a target with ``version=None`` (fresh, unfingerprinted)
# resolves to this connector through the resolver's ``versioned_over_wildcard``
# step rather than 501-ing with ``no_connector``. The versioned entry above
# always wins when both are present (resolver tie-break step 1). Mirrors the
# winsrv / rke2 wildcard rows.
register_connector_v2(
    product="wsfc",
    version="",
    impl_id="",
    cls=WsfcConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
register_typed_op_registrar(register_wsfc_typed_operations)
