# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.tempo -- TempoConnector package (#2903).

Importing this package registers :class:`TempoConnector` against the v2
connector registry under the natural key
``(product="tempo", version="2.x", impl_id="tempo-api")`` **and** the
``(product="tempo", version="", impl_id="")`` wildcard fallback -- dual
registration from day one, mirroring loki (#2235) / prometheus (#2234).

Two-phase registration, the same shape as
:mod:`meho_backplane.connectors.loki.__init__`:

* **Synchronous (import time)** -- the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` below, so
  the lookup tables are populated before the lifespan begins and a probe firing
  during startup sees a fully-populated registry.
  :func:`~meho_backplane.connectors.registry._eager_import_connectors`
  discovers this subpackage by directory name, so no manual import-list edit is
  needed elsewhere.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_tempo_typed_operations`, which delegates to
  :meth:`TempoConnector.register_operations` to upsert the six read-only
  descriptors (``tempo.search`` / ``tempo.trace`` / ``tempo.search_tags`` /
  ``tempo.search_tag_values`` / ``tempo.metrics_query_range`` /
  ``tempo.get``). Idempotent on re-call with unchanged op text.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is intentionally **not** called: Tempo has no v1 chassis history, and the
v1 entry would land as ``("tempo", "", "")`` and confuse the resolver tie-break
ladder -- the same decision loki, argocd, and pfSense made.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.tempo.connector import TempoConnector, TempoTenantRequiredError
from meho_backplane.connectors.tempo.ops import (
    TEMPO_OPS,
    TEMPO_WHEN_TO_USE_BY_GROUP,
    TempoOp,
)
from meho_backplane.connectors.tempo.read_only import (
    TempoReadOnlyError,
    assert_tempo_read_only,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_tempo_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``TempoConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault #390)
    is a module-level ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    Tempo implements the op walk as a classmethod on :class:`TempoConnector` so
    the test suite can exercise it without lifespan plumbing; this wrapper is
    the seam that lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the loki /
    prometheus contract: :func:`run_typed_op_registrars` passes the
    process-wide :class:`EmbeddingService` (or a chassis-test stub) to every
    registrar, so each registrar **must** accept the kwarg or the lifespan
    crashes with :class:`TypeError`. The wrapper accepts-and-discards it because
    :meth:`TempoConnector.register_operations` resolves the embedding service
    via ``register_typed_operation``'s process-wide singleton fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await TempoConnector.register_operations()


# v2 entry -- the canonical resolver key. The versioned triple always wins the
# resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="tempo",
    version="2.x",
    impl_id="tempo-api",
    cls=TempoConnector,
)

# Wildcard fallback -- a target with ``version=None`` (fresh, unfingerprinted,
# no operator-asserted version yet) resolves to this connector through the
# resolver's ``versioned_over_wildcard`` step rather than 501-ing with
# ``no_connector``.
register_connector_v2(
    product="tempo",
    version="",
    impl_id="",
    cls=TempoConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so
# the descriptor rows land before the first dispatch.
register_typed_op_registrar(register_tempo_typed_operations)

__all__ = [
    "TEMPO_OPS",
    "TEMPO_WHEN_TO_USE_BY_GROUP",
    "TempoConnector",
    "TempoOp",
    "TempoReadOnlyError",
    "TempoTenantRequiredError",
    "assert_tempo_read_only",
]
