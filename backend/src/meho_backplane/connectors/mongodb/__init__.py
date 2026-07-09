# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.mongodb -- MongoDbConnector package (#2237).

MEHO's second wire-protocol (non-HTTP) connector, following the DB-connector
shape the postgres connector (#2236) established. Importing this package
registers :class:`MongoDbConnector` against the v2 connector registry under the
natural key ``(product="mongodb", version="7", impl_id="mongodb-wire")`` **and**
the ``(product="mongodb", version="", impl_id="")`` wildcard fallback -- dual
registration from day one per G0.15-T6 (#1215).

Two-phase registration, the same shape as
:mod:`meho_backplane.connectors.postgres.__init__` /
:mod:`meho_backplane.connectors.loki.__init__`:

* **Synchronous (import time)** -- the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` below, so the
  lookup tables are populated before the lifespan begins and a probe firing
  during startup sees a fully-populated registry.
  :func:`~meho_backplane.connectors.registry._eager_import_connectors` discovers
  this subpackage by directory name, so no manual import-list edit is needed
  elsewhere.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_mongodb_typed_operations`, which delegates to
  :meth:`MongoDbConnector.register_operations` to upsert the eight read-only
  descriptors. Idempotent on re-call with unchanged op text.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry point
is intentionally **not** called: mongodb has no v1 chassis history, and the v1
entry would land as ``("mongodb", "", "")`` and confuse the resolver tie-break
ladder -- the same decision postgres, loki, bind9, and pfSense made.
"""

from meho_backplane.connectors.mongodb.connector import MongoDbConnector
from meho_backplane.connectors.mongodb.ops import (
    MONGO_OPS,
    MONGO_WHEN_TO_USE_BY_GROUP,
    MongoOp,
)
from meho_backplane.connectors.mongodb.session import (
    MONGO_READ_COMMANDS,
    MongoReadOnlyError,
    assert_read_command,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_mongodb_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``MongoDbConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault #390) is a
    module-level ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    MongoDB implements the op walk as a classmethod on
    :class:`MongoDbConnector` so the test suite can exercise it without lifespan
    plumbing; this wrapper is the seam that lets the standard registrar mechanism
    drive it.

    The ``embedding_service`` keyword-only parameter mirrors the postgres / loki
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so
    each registrar **must** accept the kwarg or the lifespan crashes with
    :class:`TypeError`. The wrapper accepts-and-discards it because
    :meth:`MongoDbConnector.register_operations` resolves the embedding service
    via ``register_typed_operation``'s process-wide singleton fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await MongoDbConnector.register_operations()


# v2 entry -- the canonical resolver key. The versioned triple always wins the
# resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="mongodb",
    version="7",
    impl_id="mongodb-wire",
    cls=MongoDbConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- a target with ``version=None`` (fresh,
# unfingerprinted, no operator-asserted version yet) resolves to this connector
# through the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``.
register_connector_v2(
    product="mongodb",
    version="",
    impl_id="",
    cls=MongoDbConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so the
# descriptor rows land before the first dispatch.
register_typed_op_registrar(register_mongodb_typed_operations)

__all__ = [
    "MONGO_OPS",
    "MONGO_READ_COMMANDS",
    "MONGO_WHEN_TO_USE_BY_GROUP",
    "MongoDbConnector",
    "MongoOp",
    "MongoReadOnlyError",
    "assert_read_command",
    "register_mongodb_typed_operations",
]
