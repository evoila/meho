# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.windows_dns -- WindowsDnsConnector package.

Importing the package registers :class:`WindowsDnsConnector` against the
v2 connector registry under the natural key
``(product="windns", version="2016.x", impl_id="windns-ssh")``, and
queues the connector's typed-op upserts onto the lifespan-driven
registrar list so ``endpoint_descriptor`` rows land before the first
dispatch. The package is discovered automatically by
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
(which imports every ``connectors/<product>/`` subpackage in name-sorted
order) -- there is no central connector list to edit; self-registration
at import time is the whole wiring.

Two-phase registration (same shape as
:mod:`meho_backplane.connectors.bind9.__init__` and
:mod:`meho_backplane.connectors.holodeck.__init__`):

* **Synchronous (import time)** -- the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`
  inside this module, so a probe firing during startup sees a
  fully-populated registry.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_windows_dns_typed_operations`, which delegates
  to :meth:`WindowsDnsConnector.register_operations`.

The ``(product, version, impl_id)`` triple is chosen so
``connector_id="windns-ssh-2016.x"`` round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id`
(``product`` = the first hyphen-segment of ``impl_id`` = ``"windns"``);
a hyphen/underscore in the product token would break that round-trip and
the registry's ``_assert_product_impl_id_round_trips`` guard would crash
``_eager_import_connectors`` at boot (the reason ``vcf_logs`` retired its
``vcf-logs`` token in favour of ``vrli``).

Like bind9 / Holodeck, this connector has no v1 chassis history, so the
v1 ``register_connector`` write is intentionally omitted -- only the v2
triple advertises this class.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.windows_dns import (
    ops_record_remove_preview,  # noqa: F401 -- registers the destructive-tier blast-radius preview builder at import
)
from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector
from meho_backplane.connectors.windows_dns.ops import WINDOWS_DNS_OPS, WindowsDnsOp
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_windows_dns_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``WindowsDnsConnector.register_operations``.

    The canonical typed-op registration pattern is a module-level
    ``async def register_xxx_typed_operations`` queued onto
    :func:`run_typed_op_registrars` via
    :func:`register_typed_op_registrar`. The ``embedding_service``
    keyword-only parameter mirrors the bind9 / Holodeck sibling contract
    -- ``run_typed_op_registrars`` passes the process-wide
    :class:`EmbeddingService` to every registrar, so each registrar
    **must** accept the kwarg or the lifespan crashes with
    :class:`TypeError`. The wrapper accepts-and-discards it because
    :meth:`WindowsDnsConnector.register_operations` resolves the embedding
    service via ``register_typed_operation``'s process-wide singleton
    fallback (the bind9 / Holodeck shape).
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await WindowsDnsConnector.register_operations()


__all__ = [
    "WINDOWS_DNS_OPS",
    "WindowsDnsConnector",
    "WindowsDnsOp",
    "register_windows_dns_typed_operations",
]


# v2 entry -- the canonical resolver key.
register_connector_v2(
    product="windns",
    version="2016.x",
    impl_id="windns-ssh",
    cls=WindowsDnsConnector,
)

# Wildcard fallback -- a target with ``version=None`` (fresh,
# unfingerprinted) resolves to this connector through the resolver's
# ``versioned_over_wildcard`` step rather than 501-ing with
# ``no_connector``. The versioned entry above always wins when both are
# present (resolver tie-break step 1). Mirrors the bind9 / Holodeck
# wildcard rows.
register_connector_v2(
    product="windns",
    version="",
    impl_id="",
    cls=WindowsDnsConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
register_typed_op_registrar(register_windows_dns_typed_operations)
