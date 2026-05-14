# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.kubernetes -- KubernetesConnector package.

Importing the package registers :class:`KubernetesConnector` against
both the v1 single-product registry and the v2 three-tuple registry:

* **v1 entry** -- ``register_connector("k8s", KubernetesConnector)``.
  Kept temporarily so the chassis route at
  ``POST /api/v1/connectors/{product}/{op_id}`` keeps resolving for
  the deprecation window. Removed once T11 (#412) lands the
  ``/api/v1/operations/call`` cutover.
* **v2 entry** -- ``register_connector_v2(product="k8s",
  version="1.x", impl_id="kubernetes-asyncio",
  cls=KubernetesConnector)`` (G0.6-T2 #393). The v2 entry is what the
  G0.6 dispatcher's
  :func:`~meho_backplane.connectors.resolver.resolve_connector` reads;
  the ``connector_id="kubernetes-asyncio-1.x"`` produced by
  :func:`~meho_backplane.operations._lookup.parse_connector_id`
  resolves through this entry.

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


async def register_kubernetes_typed_operations() -> None:
    """Module-level registrar wrapper for ``KubernetesConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault
    #390) is a module-level ``async def register_xxx_typed_operations``
    queued onto ``run_typed_op_registrars`` via
    :func:`register_typed_op_registrar`. K8s implements the underlying
    op walk as a classmethod on :class:`KubernetesConnector` so the
    test suite can exercise it without lifespan plumbing; this wrapper
    is the seam that lets the standard registrar mechanism drive it.
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

# v1 entry -- backward-compatible with the shipped chassis route.
# Also writes to the v2 table as ``("k8s", "", "")`` so v2-aware code
# that doesn't yet know the version/impl_id can still resolve through
# the chassis fallback. Removed when T11 #412 deprecates the chassis
# route.
register_connector("k8s", KubernetesConnector)

# v2 entry -- the canonical resolver key. Picked over the v1 fallback
# by the resolver's tie-break ladder (most-specific-version-match
# wins; this entry advertises a concrete version + impl_id, the v1
# entry advertises empty strings).
register_connector_v2(
    product="k8s",
    version="1.x",
    impl_id="kubernetes-asyncio",
    cls=KubernetesConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
# The registrar list is module-scope on
# meho_backplane.operations.typed_register; the lifespan calls
# ``run_typed_op_registrars`` after _eager_import_connectors so every
# connector subpackage has self-registered by the time the runner
# iterates. Mirrors the Vault pattern from #390 (T-Refactor-Vault).
register_typed_op_registrar(register_kubernetes_typed_operations)
