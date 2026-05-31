# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`KubernetesConnector`.

G0.6 refactor (#391) of the G3.2-T1 (#321) skeleton. The skeleton
shipped with an empty op surface and an ``unknown_op``-returning
``execute()``; this module hosts the metadata rows the connector
registers against the G0.6 substrate via
:func:`~meho_backplane.operations.typed_register.register_typed_operation`.

The shipped op surface today:

* ``k8s.about`` -- product-flavour + version snapshot built from
  :meth:`kubernetes_asyncio.client.VersionApi.get_code`. Mirrors what
  :meth:`KubernetesConnector.fingerprint` returns but goes through the
  dispatcher so callers see the same envelope (``OperationResult``)
  every other op produces. Landed with #391's refactor of the T1
  skeleton.
* ``k8s.ls`` / ``k8s.namespace.list`` / ``k8s.node.list`` -- the core
  inventory surface (G3.2-T2 #322). Metadata + helper functions live in
  :mod:`~meho_backplane.connectors.kubernetes.ops_core`; this module
  re-exports the merged tuple so registration walks a single list.
* ``k8s.pod.{list,info}`` / ``k8s.deployment.{list,info}`` -- the
  workload surface (G3.2-T3 #323). Metadata + helpers + handlers live
  in :mod:`~meho_backplane.connectors.kubernetes.ops_workload`;
  :func:`_kubernetes_ops` splats ``WORKLOAD_OPS`` into the merged tuple.
* ``k8s.service.list`` / ``k8s.ingress.list`` -- network ops
  (G3.2-T4 #324). Metadata + helpers in
  :mod:`~meho_backplane.connectors.kubernetes.ops_network`.
* ``k8s.configmap.list`` (keys-only) / ``k8s.configmap.info``
  (full data) -- config ops (G3.2-T4 #324). Metadata + helpers in
  :mod:`~meho_backplane.connectors.kubernetes.ops_config`.
* ``k8s.event.list`` -- observability op (G3.2-T4 #324). Metadata +
  helpers in :mod:`~meho_backplane.connectors.kubernetes.ops_events`
  (split out of ops_config to fit the 600-line code-quality cap).
* ``k8s.logs`` -- non-streaming pod-log fetch (G3.2-T5 #325). Metadata
  schemas + handler live in
  :mod:`~meho_backplane.connectors.kubernetes.ops_logs`; this module
  builds the registration row from those exports inside
  :func:`_kubernetes_ops`.

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
#: ``k8s.about`` is the canary op the G0.6 refactor (#391) lands against.
#: It returns a flat dict shaped like
#: :class:`~meho_backplane.connectors.schemas.FingerprintResult` but
#: goes through the dispatcher path so callers exercise the
#: register_typed_operation -> dispatch -> reduce -> audit pipeline
#: end-to-end. The G3.2-T2 (#322) core inventory ops (``k8s.ls`` /
#: ``k8s.namespace.list`` / ``k8s.node.list``) extend the tuple from
#: :mod:`~meho_backplane.connectors.kubernetes.ops_core`. The remaining
#: K8s read surface (workload / network / config / events / logs) lands
#: in #320's T3..T5 against this same registration pattern.
_K8S_ABOUT_OP = KubernetesOp(
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


def _kubernetes_ops() -> tuple[KubernetesOp, ...]:
    """Return the merged registration tuple.

    Composition: ``k8s.about`` (T1 canary) + ``CORE_OPS`` (T2 inventory:
    ``k8s.ls`` / ``k8s.namespace.list`` / ``k8s.node.list``) +
    ``WORKLOAD_OPS`` (T3 workload: ``k8s.pod.{list,info}`` /
    ``k8s.deployment.{list,info}``) + ``NETWORK_OPS`` (T4 network:
    ``k8s.service.list`` / ``k8s.ingress.list``) + ``CONFIG_OPS`` (T4
    config: ``k8s.configmap.list`` keys-only / ``k8s.configmap.info``
    full data) + ``EVENT_OPS`` (T4 observability: ``k8s.event.list``)
    + ``k8s.logs`` (T5).

    Implemented as a function call rather than a literal-and-splat at
    module level so the import order stays linear: ``ops.py`` defines
    :class:`KubernetesOp` + ``_K8S_ABOUT_OP``, then imports the T2
    inventory ops from :mod:`ops_core`, the T3 workload ops from
    :mod:`ops_workload`, the T4 network ops from :mod:`ops_network`,
    the T4 config ops from :mod:`ops_config`, the T4 event ops from
    :mod:`ops_events`, and the T5 logs op metadata from
    :mod:`ops_logs` (each of which only depends on ``KubernetesOp``
    plus its own helpers). The arrangement keeps the canary op's
    metadata co-located with the dataclass definition while letting
    the larger surfaces live in their own modules next to their
    helpers.
    """
    from meho_backplane.connectors.kubernetes.ops_config import CONFIG_OPS
    from meho_backplane.connectors.kubernetes.ops_core import CORE_OPS
    from meho_backplane.connectors.kubernetes.ops_events import EVENT_OPS
    from meho_backplane.connectors.kubernetes.ops_logs import (
        K8S_LOGS_LLM_INSTRUCTIONS,
        K8S_LOGS_PARAMETER_SCHEMA,
        K8S_LOGS_RESPONSE_SCHEMA,
    )
    from meho_backplane.connectors.kubernetes.ops_network import NETWORK_OPS
    from meho_backplane.connectors.kubernetes.ops_workload import WORKLOAD_OPS
    from meho_backplane.connectors.kubernetes.ops_write_meta import (
        WRITE_CAUTION_OPS,
        WRITE_DANGEROUS_OPS,
    )

    logs_op = KubernetesOp(
        op_id="k8s.logs",
        handler_attr="logs",
        summary="Fetch a chunk of pod logs as a single non-streaming response.",
        description=(
            "Reads container stdout/stderr via the Kubernetes API's "
            "``GET /api/v1/namespaces/{namespace}/pods/{name}/log`` route "
            "and returns the lines in one response. Honours the standard "
            "kubectl-style knobs -- ``--tail`` (default 100, capped at "
            "5000), ``--container`` (required for multi-container pods), "
            "``--since`` (duration string -- '5m', '1h', '24h', '7d'), "
            "and ``--previous`` (logs from the prior container "
            "instance after a restart). Pod name resolution accepts an "
            "exact match or a unique prefix within the namespace. The "
            "response body is capped at 1 MiB serialised; oversize "
            "payloads are truncated line-boundary from the front (most-"
            "recent lines kept) and ``truncated=true`` is set on the "
            "result with ``truncated_byte_count`` carrying the dropped "
            "byte count. Streaming (``kubectl logs -f``) is out of "
            "scope for v0.2 -- operators following live logs continue "
            "using the kubectl-vcf.sh fallback."
        ),
        parameter_schema=K8S_LOGS_PARAMETER_SCHEMA,
        response_schema=K8S_LOGS_RESPONSE_SCHEMA,
        group_key="logs",
        tags=("read-only", "pod", "logs"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_LOGS_LLM_INSTRUCTIONS,
    )

    return (
        _K8S_ABOUT_OP,
        *CORE_OPS,
        *WORKLOAD_OPS,
        *NETWORK_OPS,
        *CONFIG_OPS,
        *EVENT_OPS,
        logs_op,
        *WRITE_CAUTION_OPS,
        *WRITE_DANGEROUS_OPS,
    )


KUBERNETES_OPS: tuple[KubernetesOp, ...] = _kubernetes_ops()
