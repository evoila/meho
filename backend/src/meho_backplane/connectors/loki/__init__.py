# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.loki -- LokiConnector package (#2235).

Importing this package registers :class:`LokiConnector` against the v2
connector registry under the natural key
``(product="loki", version="3.x", impl_id="loki-api")`` **and** the
``(product="loki", version="", impl_id="")`` wildcard fallback -- dual
registration from day one per G0.15-T6 (#1215).

Two-phase registration, the same shape as
:mod:`meho_backplane.connectors.argocd.__init__` /
:mod:`meho_backplane.connectors.pfsense.__init__`:

* **Synchronous (import time)** -- the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` below, so
  the lookup tables are populated before the lifespan begins and a probe
  firing during startup sees a fully-populated registry.
  :func:`~meho_backplane.connectors.registry._eager_import_connectors`
  discovers this subpackage by directory name, so no manual import-list edit
  is needed elsewhere.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_loki_typed_operations`, which delegates to
  :meth:`LokiConnector.register_operations` to upsert the six read-only
  descriptors (``loki.query`` / ``loki.query_range`` / ``loki.labels`` /
  ``loki.label_values`` / ``loki.series`` / ``loki.get``). Idempotent on
  re-call with unchanged op text.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is intentionally **not** called: Loki has no v1 chassis history, and the
v1 entry would land as ``("loki", "", "")`` and confuse the resolver tie-break
ladder -- the same decision argocd, bind9, and pfSense made.
"""

from meho_backplane.connectors.loki.connector import LokiConnector, LokiTenantRequiredError
from meho_backplane.connectors.loki.ops import (
    LOKI_OPS,
    LOKI_WHEN_TO_USE_BY_GROUP,
    LokiOp,
)
from meho_backplane.connectors.loki.read_only import (
    LokiReadOnlyError,
    assert_loki_read_only,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_loki_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``LokiConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault #390)
    is a module-level ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    Loki implements the op walk as a classmethod on :class:`LokiConnector` so
    the test suite can exercise it without lifespan plumbing; this wrapper is
    the seam that lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the argocd / bind9
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so
    each registrar **must** accept the kwarg or the lifespan crashes with
    :class:`TypeError`. The wrapper accepts-and-discards it because
    :meth:`LokiConnector.register_operations` resolves the embedding service
    via ``register_typed_operation``'s process-wide singleton fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await LokiConnector.register_operations()


# v2 entry -- the canonical resolver key. The versioned triple always wins the
# resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="loki",
    version="3.x",
    impl_id="loki-api",
    cls=LokiConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- a target with ``version=None`` (fresh,
# unfingerprinted, no operator-asserted version yet) resolves to this connector
# through the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``.
register_connector_v2(
    product="loki",
    version="",
    impl_id="",
    cls=LokiConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so
# the descriptor rows land before the first dispatch.
register_typed_op_registrar(register_loki_typed_operations)

__all__ = [
    "LOKI_OPS",
    "LOKI_WHEN_TO_USE_BY_GROUP",
    "LokiConnector",
    "LokiOp",
    "LokiReadOnlyError",
    "LokiTenantRequiredError",
    "assert_loki_read_only",
    "register_loki_typed_operations",
]
