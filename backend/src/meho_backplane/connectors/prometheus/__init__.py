# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.prometheus -- PrometheusConnector package.

Initiative #2228 / Task #2234. Importing this package registers
:class:`PrometheusConnector` against the v2 connector registry under the
natural key ``(product="prometheus", version="2.x", impl_id="prometheus-api")``
**and** the ``(product="prometheus", version="", impl_id="")`` wildcard
fallback -- dual registration per G0.15-T6 (#1215), the same shape
:mod:`~meho_backplane.connectors.bind9` and
:mod:`~meho_backplane.connectors.pfsense` use.

One connector serves the three PromQL-HTTP-compatible metrics backends the
estate runs -- Prometheus, Thanos Query, and Grafana Mimir/Cortex -- all
over the shared ``/api/v1`` HTTP API. The connector is read-only by
construction (GET-only + ``/api/v1/`` path-allowlist); see the connector
module docstring.

Two-phase registration:

* **Synchronous (import time)** -- the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` so the
  lookup tables are populated before the lifespan begins and a probe
  firing during startup sees a fully-populated registry. The
  :func:`~meho_backplane.connectors.registry._eager_import_connectors` walk
  discovers this subpackage by directory name -- no manual import-list
  edit is needed elsewhere.

* **Asynchronous (lifespan startup)** --
  :func:`register_prometheus_typed_operations` delegates to
  :meth:`PrometheusConnector.register_operations`, which upserts the eight
  read-only ``endpoint_descriptor`` rows (``prometheus.query`` /
  ``query_range`` / ``series`` / ``labels`` / ``targets`` / ``rules`` /
  ``alerts`` / ``get``).

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is intentionally **not** called: prometheus has no v1 chassis
history, and the v1 entry would land as ``("prometheus", "", "")`` and
confuse the resolver tie-break ladder. Only the v2 triple (+ wildcard)
advertises this class -- the same decision bind9, argocd, harbor, and
hetzner made.
"""

from meho_backplane.connectors.prometheus.connector import (
    PrometheusConnector,
    PrometheusReadOnlyError,
    PrometheusSecretLoader,
)
from meho_backplane.connectors.prometheus.ops import (
    PROMETHEUS_OPS,
    PROMETHEUS_WHEN_TO_USE_BY_GROUP,
    PrometheusOp,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_prometheus_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``PrometheusConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault
    #390) is a module-level ``async def register_xxx_typed_operations``
    queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    via :func:`register_typed_op_registrar`. prometheus implements the
    underlying op walk as a classmethod on :class:`PrometheusConnector`
    (so the test suite can exercise it without lifespan plumbing); this
    wrapper is the seam that lets the standard registrar mechanism drive
    it.

    The ``embedding_service`` keyword-only parameter mirrors the argocd /
    bind9 / K8s sibling contract: :func:`run_typed_op_registrars` passes
    the process-wide :class:`EmbeddingService` (or a chassis-test stub) to
    every registrar via ``registrar(embedding_service=...)``, so each
    registrar **must** accept the kwarg or the lifespan crashes with
    :class:`TypeError`. The wrapper accepts-and-discards it because
    :meth:`PrometheusConnector.register_operations` resolves the embedding
    service via ``register_typed_operation``'s process-wide singleton
    fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await PrometheusConnector.register_operations()


# v2 entry -- the canonical resolver key. The versioned triple always wins
# the resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="prometheus",
    version="2.x",
    impl_id="prometheus-api",
    cls=PrometheusConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- a target with ``version=None``
# (fresh, unfingerprinted, no operator-asserted version yet) resolves to
# this connector through the resolver's ``versioned_over_wildcard`` step
# rather than 501-ing with ``no_connector``.
register_connector_v2(
    product="prometheus",
    version="",
    impl_id="",
    cls=PrometheusConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The
# runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the descriptor rows land before the first
# dispatch.
register_typed_op_registrar(register_prometheus_typed_operations)

__all__ = [
    "PROMETHEUS_OPS",
    "PROMETHEUS_WHEN_TO_USE_BY_GROUP",
    "PrometheusConnector",
    "PrometheusOp",
    "PrometheusReadOnlyError",
    "PrometheusSecretLoader",
    "register_prometheus_typed_operations",
]
