# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.rabbitmq — RabbitMqConnector package (#2233).

Importing this package registers :class:`RabbitMqConnector` against the v2
connector registry under the natural key
``(product="rabbitmq", version="3.x", impl_id="rabbitmq-management")``
**and** the ``(product="rabbitmq", version="", impl_id="")`` wildcard
fallback — dual registration per G0.15-T6 (#1215).

Two-phase registration (same shape as
:mod:`meho_backplane.connectors.argocd.__init__`):

* **Synchronous (import time)** — the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` so the
  lookup tables are populated before the lifespan begins and a probe
  firing during startup sees a fully-populated registry. The
  :func:`~meho_backplane.connectors.registry._eager_import_connectors`
  walk discovers this subpackage by directory name, so no manual
  import-list edit is needed elsewhere.
* **Asynchronous (lifespan startup)** —
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_rabbitmq_typed_operations`, which delegates to
  :meth:`RabbitMqConnector.register_operations` (walks
  :data:`~meho_backplane.connectors.rabbitmq.ops.RABBITMQ_OPS` and upserts
  the read-only descriptors). Async because the helper writes to the DB
  and the embedding-text encode step is async. Idempotent on re-call.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is intentionally **not** called: RabbitMQ has no v1 chassis
history, so only the v2 triple (+ wildcard) advertises this class — the
same decision Harbor, bind9, NSX, ArgoCD made.
"""

from meho_backplane.connectors.rabbitmq.connector import (
    RabbitMqConnector,
    RabbitMqMethodNotAllowedError,
)
from meho_backplane.connectors.rabbitmq.ops import (
    RABBITMQ_OPS,
    RABBITMQ_REDACTED_OP_IDS,
    RABBITMQ_WHEN_TO_USE_BY_GROUP,
    RabbitMqOp,
)
from meho_backplane.connectors.rabbitmq.redact import redact_rabbitmq_payload
from meho_backplane.connectors.rabbitmq.session import (
    RABBITMQ_CREDENTIAL_FIELDS,
    RabbitMqCredentialsLoader,
    RabbitMqTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_rabbitmq_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``RabbitMqConnector.register_operations``.

    The canonical typed-op registration pattern is a module-level ``async
    def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    via :func:`register_typed_op_registrar`. RabbitMQ implements the op
    walk as a classmethod on :class:`RabbitMqConnector` (so the test suite
    can exercise it without lifespan plumbing); this wrapper is the seam
    that lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the ArgoCD /
    bind9 sibling contract: :func:`run_typed_op_registrars` passes the
    process-wide :class:`EmbeddingService` (or a chassis-test stub) to
    every registrar, so each registrar **must** accept the kwarg or the
    lifespan crashes with :class:`TypeError`. The wrapper accepts-and-
    discards it because :meth:`RabbitMqConnector.register_operations`
    resolves the embedding service via ``register_typed_operation``'s
    process-wide singleton fallback.
    """
    del embedding_service  # see docstring — kwarg accepted for runner-compatibility
    await RabbitMqConnector.register_operations()


# v2 entry — the canonical resolver key. The versioned triple always wins
# the resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="rabbitmq",
    version="3.x",
    impl_id="rabbitmq-management",
    cls=RabbitMqConnector,
)

# G0.15-T6 (#1215) wildcard fallback — a target with ``version=None``
# (fresh, unfingerprinted, no operator-asserted version yet) resolves to
# this connector through the resolver's ``versioned_over_wildcard`` step
# rather than 501-ing with ``no_connector``.
register_connector_v2(
    product="rabbitmq",
    version="",
    impl_id="",
    cls=RabbitMqConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The
# runner iterates after ``_eager_import_connectors`` so the descriptor
# rows land before the first dispatch.
register_typed_op_registrar(register_rabbitmq_typed_operations)

__all__ = [
    "RABBITMQ_CREDENTIAL_FIELDS",
    "RABBITMQ_OPS",
    "RABBITMQ_REDACTED_OP_IDS",
    "RABBITMQ_WHEN_TO_USE_BY_GROUP",
    "RabbitMqConnector",
    "RabbitMqCredentialsLoader",
    "RabbitMqMethodNotAllowedError",
    "RabbitMqOp",
    "RabbitMqTargetLike",
    "load_credentials_from_vault",
    "redact_rabbitmq_payload",
    "register_rabbitmq_typed_operations",
]
