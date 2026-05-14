# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`KubernetesConnector`.

G0.6 refactor (#391) of the G3.2-T1 (#321) skeleton. The skeleton
shipped with an empty op surface and an ``unknown_op``-returning
``execute()``; this module is the first concrete op the connector
registers against the G0.6 substrate via
:func:`~meho_backplane.operations.typed_register.register_typed_operation`.

The op surface is intentionally minimal in this Initiative:

* ``k8s.about`` -- product-flavour + version snapshot built from
  :meth:`kubernetes_asyncio.client.VersionApi.get_code`. Mirrors what
  :meth:`KubernetesConnector.fingerprint` returns but goes through the
  dispatcher so callers see the same envelope (``OperationResult``)
  every other op produces. The full 13-op K8s surface (``k8s.pod.list``
  / ``k8s.deployment.list`` / ...) lands in G3.2-T2..T5 (#320 work
  items) against the same registration pattern from the start.

Each op is declared in :data:`KUBERNETES_OPS` as a typed-metadata row.
The connector's ``register_operations()`` classmethod walks the list
once per process at lifespan startup and upserts the descriptors
through :func:`register_typed_operation`. The handler itself is a
bound method on :class:`KubernetesConnector` so the descriptor's
``handler_ref`` round-trips through the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler`
walk -- the same shape every G3.x typed connector will adopt.

References
----------
* Parent Initiative: #388 G0.6 work item 11 (refactor shipped Vault +
  K8s connectors to ``register_typed_operation()``).
* G3.2-T1 #321 (skeleton this builds on).
* G3.2 #320 (the full K8s op surface that consumes this pattern).
* :data:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector.product`
  (``"k8s"`` per the registry v2 entry; the v1 entry under
  ``"kubernetes"`` is kept for backward compat with the chassis route).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["KUBERNETES_OPS", "KubernetesOp"]


@dataclass(frozen=True)
class KubernetesOp:
    """Metadata for one K8s op the connector registers at startup.

    The fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod
    can splat the dataclass into the helper without per-op
    boilerplate.

    ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
    that exposes the async handler. The connector resolves the bound
    method against itself at registration time so
    :func:`~meho_backplane.operations.typed_register.derive_handler_ref`
    serialises a ``module.ClassName.method`` dotted path the dispatcher
    can resolve back via :func:`importlib.import_module` + chained
    :func:`getattr`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: The ops :class:`KubernetesConnector` registers at lifespan startup.
#:
#: ``k8s.about`` is the canary op the G0.6 refactor (#391) lands
#: against. It returns a flat dict shaped like
#: :class:`~meho_backplane.connectors.schemas.FingerprintResult` but
#: goes through the dispatcher path so callers exercise the
#: register_typed_operation -> dispatch -> reduce -> audit pipeline
#: end-to-end. The full 13-op K8s read surface lands in #320's T2..T5
#: against this same registration pattern.
KUBERNETES_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.about",
        handler_attr="about",
        summary="Return the cluster's product flavour, version, and platform.",
        description=(
            "Hits the Kubernetes API server's ``GET /version`` endpoint via "
            "``kubernetes_asyncio.client.VersionApi.get_code`` and returns a "
            "flat dict with the cluster's product slug (rke2 / k3s / eks / "
            "gke / aks / vanilla derived from the gitVersion suffix), full "
            "git_version, build date, and platform string. Use to identify "
            "the cluster before issuing higher-level ops or to populate "
            "operator dashboards. No params; works against any target whose "
            "kubeconfig the operator's tenant can read."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "git_version": {"type": "string"},
                "build_date": {"type": "string"},
                "major": {"type": "string"},
                "minor": {"type": "string"},
                "platform": {"type": "string"},
                "go_version": {"type": "string"},
                "git_commit": {"type": "string"},
                "git_tree_state": {"type": "string"},
            },
            "required": ["product", "git_version"],
            "additionalProperties": True,
        },
        group_key="cluster",
        tags=("read-only", "cluster", "identity"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call when the operator wants to identify the K8s cluster "
                "behind a target before issuing a higher-level op (pod list, "
                "deployment scale, etc.), or when the agent needs to pick a "
                "version-flavoured doc page from the knowledge base."
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict; the ``product`` field is the canonical slug "
                "(``rke2`` / ``k3s`` / ``eks`` / ``gke`` / ``aks`` / "
                "``vanilla``). ``git_version`` carries the full v-prefixed "
                "string including distribution suffix."
            ),
        },
    ),
)
