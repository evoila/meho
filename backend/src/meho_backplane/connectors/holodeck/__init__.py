# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.holodeck -- HolodeckConnector package.

Importing the package registers :class:`HolodeckConnector` against
the v2 connector registry under the natural key
``(product="holodeck", version="9.0", impl_id="holodeck-ssh")``, and
queues the connector's typed-op upserts onto the lifespan-driven
registrar list so ``endpoint_descriptor`` rows land before the first
dispatch.

Two-phase registration (same shape as
:mod:`meho_backplane.connectors.bind9.__init__` and
:mod:`meho_backplane.connectors.pfsense.__init__`):

* **Synchronous (import time)** -- the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`
  inside this module. The registry lookup tables are populated
  before the lifespan begins so a probe firing during startup sees
  a fully-populated registry.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_holodeck_typed_operations`, which delegates
  to :meth:`HolodeckConnector.register_operations`. Async because
  the helper writes to the DB and the embedding-text encode step is
  async. Idempotent on re-call with unchanged op text.

The Holodeck connector intentionally does **not** call the v1
``register_connector`` -- the v1 entry path is for chassis-route
backwards compatibility (the deprecated ``POST /api/v1/connectors/
{product}/{op_id}`` route removed by G0.6-T11 #412), and Holodeck
has never shipped behind it. Skipping the v1 write keeps the
resolver tie-break ladder unambiguous: only the v2 triple advertises
this class.
"""

from meho_backplane.connectors.holodeck.connector import HolodeckConnector
from meho_backplane.connectors.holodeck.ops import HOLODECK_OPS, HolodeckOp
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_holodeck_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``HolodeckConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-
    Vault #390) is a module-level ``async def
    register_xxx_typed_operations`` queued onto
    :func:`run_typed_op_registrars` via
    :func:`register_typed_op_registrar`. Holodeck implements the
    underlying op walk as a classmethod on :class:`HolodeckConnector`
    so the test suite can exercise it without lifespan plumbing;
    this wrapper is the seam that lets the standard registrar
    mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the
    bind9 / pfSense sibling contract (#463) -- ``run_typed_op_registrars``
    passes the process-wide :class:`EmbeddingService` (or a chassis-
    test stub) to every registrar via ``registrar(embedding_service=...)``,
    so each registrar **must** accept the kwarg or the lifespan
    crashes with :class:`TypeError`. The wrapper currently does not
    forward the value because
    :meth:`HolodeckConnector.register_operations` resolves the
    embedding service via
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`'s
    process-wide singleton fallback; the kwarg-accept-and-discard
    shape matches the bind9 / pfSense siblings.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await HolodeckConnector.register_operations()


__all__ = [
    "HOLODECK_OPS",
    "HolodeckConnector",
    "HolodeckOp",
    "register_holodeck_typed_operations",
]


# v2 entry -- the canonical resolver key. Holodeck has no v1 chassis
# history, so the v1 ``register_connector`` write is intentionally
# omitted (see module docstring).
register_connector_v2(
    product="holodeck",
    version="9.0",
    impl_id="holodeck-ssh",
    cls=HolodeckConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
# The registrar list is module-scope on
# meho_backplane.operations.typed_register; the lifespan calls
# ``run_typed_op_registrars`` after _eager_import_connectors so every
# connector subpackage has self-registered by the time the runner
# iterates.
register_typed_op_registrar(register_holodeck_typed_operations)
