# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.kubernetes -- KubernetesConnector package.

Importing the package registers :class:`KubernetesConnector` against
both the v1 single-product registry and the v2 three-tuple registry:

* **v1 entry** -- ``register_connector("k8s", KubernetesConnector)``.
  Retained for ``get_connector("k8s")`` callers (Kubernetes resolver
  tests, the ``/api/v1/health`` Vault federation probe shape, and
  startup checks). The chassis route that originally motivated it
  (``POST /api/v1/connectors/{product}/{op_id}``) was deprecated and
  removed by G0.6-T11 (#412); ``POST /api/v1/operations/call`` is the
  canonical dispatch surface.
* **v2 entry** -- ``register_connector_v2(product="k8s",
  version="1.x", impl_id="k8s", cls=KubernetesConnector)``
  (G0.6-T2 #393). The v2 entry is what the G0.6 dispatcher's
  :func:`~meho_backplane.connectors.resolver.resolve_connector` reads;
  the ``connector_id="k8s-1.x"`` produced by
  :func:`~meho_backplane.operations._lookup.parse_connector_id`
  resolves through this entry. The ``impl_id == product`` shape
  mirrors the Vault sibling (single-impl typed connector pattern --
  the library name ``kubernetes_asyncio`` lives in the package layout
  + ``pyproject.toml`` dependency, not the registry's natural-key
  triple).

The registry is imported eagerly at app startup via
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
(called from the FastAPI lifespan hook). Operation registration
(``register_typed_operation`` for every row in
:data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`) runs
via :meth:`KubernetesConnector.register_operations` from the lifespan
hook *after* the eager import, because it needs an active async DB
session.
"""

from meho_backplane.connectors.kubernetes.connector import (
    KubernetesConnector,
    product_from_git_version,
)
from meho_backplane.connectors.kubernetes.kubeconfig import (
    KubeconfigLoader,
    KubernetesTargetLike,
    load_kubeconfig_from_vault,
    parse_kubeconfig_yaml,
)
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS, KubernetesOp
from meho_backplane.connectors.registry import (
    register_connector,
    register_connector_v2,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_kubernetes_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``KubernetesConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault
    #390) is a module-level ``async def register_xxx_typed_operations``
    queued onto ``run_typed_op_registrars`` via
    :func:`register_typed_op_registrar`. K8s implements the underlying
    op walk as a classmethod on :class:`KubernetesConnector` so the
    test suite can exercise it without lifespan plumbing; this wrapper
    is the seam that lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the
    :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
    contract (#463) -- :func:`run_typed_op_registrars` passes the
    process-wide :class:`EmbeddingService` (or a chassis-test stub) to
    every registrar via ``registrar(embedding_service=...)``, so each
    registrar **must** accept the kwarg or the lifespan crashes with
    ``TypeError`` (see Task #475 for the post-#461/#463 ordering
    incident this signature repairs). The wrapper currently does not
    forward the value because
    :meth:`KubernetesConnector.register_operations` resolves the
    embedding service via :func:`register_typed_operation`'s
    process-wide singleton fallback; end-to-end threading (so chassis
    tests can stub the singleton for K8s-registrar-specific runs) is a
    v0.2.next refinement called out in #475's Out-of-scope.
    """
    await KubernetesConnector.register_operations()


__all__ = [
    "KUBERNETES_OPS",
    "KubeconfigLoader",
    "KubernetesConnector",
    "KubernetesOp",
    "KubernetesTargetLike",
    "load_kubeconfig_from_vault",
    "parse_kubeconfig_yaml",
    "product_from_git_version",
    "register_kubernetes_typed_operations",
]

# v1 entry -- retained for ``get_connector("k8s")`` callers (the
# ``/api/v1/targets/{name}/probe`` route at api/v1/targets.py reads
# the v1 registry to fingerprint a target before any dispatch path
# runs). Also writes to the v2 table as ``("k8s", "", "")``. The
# v1 chassis dispatch route was removed by G0.6-T11 (#412); the v1
# table itself stays because the probe route still keys on it.
register_connector("k8s", KubernetesConnector)

# v2 entry -- the canonical resolver key. ``connector_id="k8s-1.x"``
# resolves through this entry. The resolver's tie-break ladder
# (G0.14-T2 #1143 step 1, ``versioned_over_wildcard``) demotes the v1
# wildcard ``("k8s", "", "")`` whenever this versioned entry is also
# a candidate, so an unfingerprinted K8s target resolves cleanly to
# the versioned class instead of bailing with
# ``AmbiguousConnectorResolution``. The wildcard still wins when it
# is the only candidate (no versioned entry registered for the
# target's product), preserving the v1-only resolution path.
register_connector_v2(
    product="k8s",
    version="1.x",
    impl_id="k8s",
    cls=KubernetesConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
# The registrar list is module-scope on
# meho_backplane.operations.typed_register; the lifespan calls
# ``run_typed_op_registrars`` after _eager_import_connectors so every
# connector subpackage has self-registered by the time the runner
# iterates. Mirrors the Vault pattern from #390 (T-Refactor-Vault).
register_typed_op_registrar(register_kubernetes_typed_operations)
