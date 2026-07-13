# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.rke2 -- Rke2SshConnector package.

Importing the package registers :class:`Rke2SshConnector` against the v2
connector registry under the natural key
``(product="rke2", version="1.x", impl_id="rke2-ssh")``, and queues the
connector's typed-op upserts onto the lifespan-driven registrar list so
``endpoint_descriptor`` rows land before the first dispatch.

Two-phase registration (same shape as
:mod:`meho_backplane.connectors.bind9.__init__` and
:mod:`meho_backplane.connectors.holodeck.__init__`):

* **Synchronous (import time)** -- the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`
  inside this module, so a probe firing during startup sees a
  fully-populated registry.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_rke2_typed_operations`, which delegates to
  :meth:`Rke2SshConnector.register_operations`. Async because the helper
  writes to the DB and the embedding-text encode step is async.
  Idempotent on re-call with unchanged op text.

The connector intentionally does **not** call the v1
``register_connector`` -- the v1 entry path is for chassis-route
backwards compatibility, and RKE2 has never shipped behind it. Skipping
the v1 write keeps the resolver tie-break ladder unambiguous: only the
v2 triple advertises this class.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.rke2.connector import Rke2SshConnector
from meho_backplane.connectors.rke2.ops import RKE2_OPS, Rke2Op
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_rke2_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``Rke2SshConnector.register_operations``.

    The canonical typed-op registration pattern is a module-level
    ``async def register_xxx_typed_operations`` queued onto
    :func:`run_typed_op_registrars` via
    :func:`register_typed_op_registrar`. RKE2 implements the underlying op
    walk as a classmethod on :class:`Rke2SshConnector` so the test suite
    can exercise it without lifespan plumbing; this wrapper is the seam
    that lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the bind9 /
    holodeck sibling contract (#463) -- :func:`run_typed_op_registrars`
    passes the process-wide :class:`EmbeddingService` (or a chassis-test
    stub) to every registrar via ``registrar(embedding_service=...)``, so
    each registrar **must** accept the kwarg or the lifespan crashes with
    :class:`TypeError`. The wrapper accepts and discards it because
    :meth:`Rke2SshConnector.register_operations` resolves the embedding
    service via ``register_typed_operation``'s process-wide singleton
    fallback -- the kwarg-accept-and-discard shape matches the siblings.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await Rke2SshConnector.register_operations()


__all__ = [
    "RKE2_OPS",
    "Rke2Op",
    "Rke2SshConnector",
    "register_rke2_typed_operations",
]


# v2 entry -- the canonical resolver key. RKE2 has no v1 chassis history,
# so the v1 ``register_connector`` write is intentionally omitted.
register_connector_v2(
    product="rke2",
    version="1.x",
    impl_id="rke2-ssh",
    cls=Rke2SshConnector,
)

# Wildcard fallback -- a target with ``version=None`` (fresh,
# unfingerprinted, no operator-asserted version yet) resolves to this
# connector through the resolver's ``versioned_over_wildcard`` step
# rather than 501-ing with ``no_connector``. The versioned entry above
# always wins when both are present (resolver tie-break step 1).
register_connector_v2(
    product="rke2",
    version="",
    impl_id="",
    cls=Rke2SshConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
register_typed_op_registrar(register_rke2_typed_operations)
