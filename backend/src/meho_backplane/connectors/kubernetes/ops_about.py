# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``k8s.about`` row: the cluster's product flavour, version and platform.

The handler is :meth:`KubernetesConnector.about
<meho_backplane.connectors.kubernetes.connector.KubernetesConnector.about>`.
It returns a flat dict shaped like
:class:`~meho_backplane.connectors.schemas.FingerprintResult`, but it goes
through the dispatcher, so callers get the same envelope as every other op.
:func:`~meho_backplane.connectors.kubernetes.ops.collect_ops` reads
:data:`OPS` at start-up.
"""

from __future__ import annotations

from meho_backplane.connectors.kubernetes.ops import KubernetesOp

__all__ = ["ABOUT_OP", "OPS"]

ABOUT_OP = KubernetesOp(
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
)


#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = (ABOUT_OP,)
