# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Core K8s inventory ops -- ``k8s.ls`` / ``k8s.namespace.list`` / ``k8s.node.list``.

G3.2-T2 (#322) of Initiative #320. T1's refactor (#391) landed the
:class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
skeleton plus the canary ``k8s.about`` op against the G0.6 substrate;
this module adds three operator-facing inventory ops to that surface:

* ``k8s.ls [path]`` -- synthetic walker mirroring ``govc ls /``. Three
  shapes:

  - ``k8s.ls /`` returns the cluster-level inventory: namespace names +
    a small fixed list of cluster-scoped kinds.
  - ``k8s.ls /<namespace>`` returns the kind -> count summary for the
    namespace, using the fixed list of "commonly relevant" kinds from
    :data:`K8S_NAMESPACED_KIND_LISTERS` so an ``ls`` call costs N small
    "list 1 item, read remaining_item_count" round-trips rather than
    pulling every row of every kind.
  - ``k8s.ls /<namespace>/<kind>`` forwards through the connector's
    dispatcher shim to ``k8s.<kind>.list`` for that namespace. Kinds
    whose ``list`` op hasn't been registered yet (T3+ ships pod /
    deployment / service / ingress / configmap / event) come back
    through the shim's structured ``unknown_op`` envelope -- the
    forwarder doesn't pretend to know which kinds will exist when.

* ``k8s.namespace.list`` -- ``CoreV1Api.list_namespace()``. Returns one
  row per namespace with name / phase / age / labels.

* ``k8s.node.list`` -- ``CoreV1Api.list_node()``. Returns one row per
  node with name / status / roles (derived from
  ``node-role.kubernetes.io/<role>`` label keys) / version / kernel /
  os / internal IP / taints.

JSONFlux handle pattern (Issue #322 acceptance criterion)
---------------------------------------------------------

The Issue body's "Handle threshold tested: against k3d populated with
50+ namespaces, sample of 20 + handle returned" criterion assumed the
shared :class:`HandleStore` from G3.1-T4 (#304) would be in place. #304
was **superseded** (closed without a HandleStore landing -- the
Initiative-redraft note on the issue spells this out). The substrate
now in the tree ships
:class:`meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(G0.6.1 #750), installed as the dispatcher default in ``main.py`` via
``set_default_reducer`` -- it materializes set-shaped responses into a
:class:`~meho_backplane.connectors.schemas.ResultHandle`.

The handlers in this module emit **raw row lists** in the response
dict, exactly the shape the default JsonFluxReducer reduces. That
reducer -- not the connector -- owns the threshold check, the row
truncation, the spill to MinIO/S3/Valkey, and the ``ResultHandle``
construction. Putting threshold logic per-handler would couple every
connector to the reducer's calibration choice and double-implement
the spill path; per the substrate split documented on
:mod:`meho_backplane.operations.reducer`, set-shaped reduction is the
reducer's job, not the connector's. See ``docs/architecture/jsonflux.md``.

A small forward-compat marker stays in the response envelope so future
agent prompts (which inline ``llm_instructions.output_shape``) know
where the row list lives: the ``rows`` key is the inventory; the
``total`` key carries the un-truncated count from the server's
``V1ListMeta.remaining_item_count``-aware view; the reducer reads both
and produces the handle when its threshold trips.

References
----------
* Parent Initiative: #320 (G3.2 K8s connector).
* Substrate: #388 G0.6 (typed-op registry + dispatcher), #391 (the
  T1 refactor this builds on).
* Sibling: #304 closed-superseded HandleStore Task -- documents why
  the handle is reducer-side, not connector-side.
* :mod:`kubernetes_asyncio.client` ``CoreV1Api`` reference:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1Namespace, V1Node, V1Taint

__all__ = [
    "CORE_OPS",
    "K8S_LS_LLM_INSTRUCTIONS",
    "K8S_LS_PARAMETER_SCHEMA",
    "K8S_NAMESPACED_KIND_LISTERS",
    "K8S_NAMESPACE_LIST_LLM_INSTRUCTIONS",
    "K8S_NODE_LIST_LLM_INSTRUCTIONS",
    "age_seconds",
    "namespace_row",
    "node_row",
    "taint_row",
]


# ---------------------------------------------------------------------------
# Row-shape helpers -- pure mappings over kubernetes_asyncio model objects.
# Kept here (rather than inside the handlers) so the test suite can pin the
# wire shape against synthetic fixtures without booting an event loop.
# ---------------------------------------------------------------------------


def age_seconds(creation_timestamp: datetime | None, *, now: datetime | None = None) -> int | None:
    """Return seconds elapsed between *creation_timestamp* and *now*.

    ``None`` when *creation_timestamp* is ``None`` (Kubernetes sets it on
    every persisted object, but the typed model exposes it as optional so
    callers handling a freshly-built-in-memory object don't crash). The
    return is **integer seconds** -- ops surfaces sub-second precision
    only for latency telemetry, never for object age.

    ``now`` is parameterised for test-determinism; production paths leave
    it ``None`` and the helper resolves :func:`datetime.now` at call time.
    Both timestamps are normalised to UTC -- the k8s API server emits
    RFC 3339 timestamps with a ``Z`` suffix that
    :mod:`kubernetes_asyncio` parses into tz-aware ``datetime`` objects,
    so the subtraction is well-defined without an explicit tz cast.
    """
    if creation_timestamp is None:
        return None
    reference = now if now is not None else datetime.now(UTC)
    delta = reference - creation_timestamp
    # ``total_seconds()`` can return a float when sub-second precision
    # survives the wire-format round-trip; the integer floor matches the
    # operator-facing "how old is this thing?" intent (a 12.4-second-old
    # namespace reads as "12 seconds old", not "12.4").
    return int(delta.total_seconds())


def namespace_row(ns: V1Namespace, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Namespace` into the op's row shape.

    Pure function -- given the same model object, returns identical
    output. The ``now`` seam is for deterministic tests of the
    ``age_seconds`` derivation; production callers leave it ``None``.
    """
    metadata = ns.metadata
    status = ns.status
    return {
        "name": metadata.name if metadata is not None else None,
        # ``status.phase`` is the operator-visible string ("Active" /
        # "Terminating"). Some test fixtures (and the brief window during
        # namespace creation before the controller writes status) leave it
        # ``None``; surface that verbatim rather than coercing to "Unknown"
        # so the operator sees the real state.
        "status": status.phase if status is not None else None,
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        # ``labels`` is the raw dict the API server returned; ``{}`` when
        # the namespace has no labels. ``kubernetes_asyncio`` exposes a
        # missing labels block as ``None``; coerce to an empty dict so the
        # row's type is stable for downstream agents / CLI renderers.
        "labels": (metadata.labels or {}) if metadata is not None else {},
    }


def taint_row(taint: V1Taint) -> dict[str, Any]:
    """Project a :class:`V1Taint` into a flat dict the agent surface renders."""
    return {
        "key": taint.key,
        "value": taint.value,
        "effect": taint.effect,
    }


def _node_roles(labels: dict[str, str] | None) -> list[str]:
    """Derive the node's role list from its label set.

    Kubernetes encodes node role membership via the
    ``node-role.kubernetes.io/<role>`` label-key convention (the value is
    typically the empty string -- the **presence** of the key is the
    signal). Some distributions also use ``kubernetes.io/role``; that
    legacy single-role label is folded into the same list so the row
    shape stays uniform across RKE2 / K3s / EKS / GKE.

    Returns a sorted list so the wire output is deterministic across
    repeated calls and across the unordered Python-dict iteration order.
    """
    if not labels:
        return []
    roles: set[str] = set()
    role_prefix = "node-role.kubernetes.io/"
    legacy_key = "kubernetes.io/role"
    for key, value in labels.items():
        if key.startswith(role_prefix):
            suffix = key[len(role_prefix) :]
            if suffix:
                roles.add(suffix)
        elif key == legacy_key and value:
            roles.add(value)
    return sorted(roles)


def _node_internal_ip(addresses: list[Any] | None) -> str | None:
    """Pluck the ``InternalIP`` entry from a node's status addresses.

    The API server returns ``addresses`` as a list of ``V1NodeAddress``
    objects with ``type`` in ``{"InternalIP", "ExternalIP", "Hostname"}``.
    Operators want the InternalIP for SSH-into-node workflows; the other
    types are informational. ``None`` when the node has no InternalIP
    entry (rare; some kubelet configurations omit it).
    """
    if not addresses:
        return None
    for addr in addresses:
        if addr.type == "InternalIP":
            address: str | None = addr.address
            return address
    return None


def node_row(node: V1Node, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Node` into the op's row shape.

    The status is derived from the node's condition list: a "Ready"
    condition with ``status == "True"`` maps to ``"Ready"``; any other
    condition state maps to ``"NotReady"``. This mirrors what
    ``kubectl get nodes`` prints, which is what operators are
    used to.
    """
    metadata = node.metadata
    status = node.status
    spec = node.spec

    # Ready condition -> status string. The K8s API returns conditions as
    # a list; ``kubectl get nodes`` shows ``Ready`` when the condition's
    # ``status`` is exactly ``"True"`` (the string, not the bool), and
    # ``NotReady`` otherwise. Mirror that mapping so the operator sees
    # familiar output.
    status_str: str = "Unknown"
    if status is not None and status.conditions:
        for cond in status.conditions:
            if cond.type == "Ready":
                status_str = "Ready" if cond.status == "True" else "NotReady"
                break

    node_info = status.node_info if status is not None else None
    taints = (spec.taints or []) if spec is not None else []
    return {
        "name": metadata.name if metadata is not None else None,
        "status": status_str,
        "roles": _node_roles(metadata.labels if metadata is not None else None),
        "version": node_info.kubelet_version if node_info is not None else None,
        "kernel": node_info.kernel_version if node_info is not None else None,
        "os": node_info.operating_system if node_info is not None else None,
        "internal_ip": _node_internal_ip(status.addresses if status is not None else None),
        "taints": [taint_row(t) for t in taints],
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        "labels": (metadata.labels or {}) if metadata is not None else {},
    }


# ---------------------------------------------------------------------------
# Fixed kind list ``k8s.ls /<namespace>`` queries.
#
# The Issue body documents the "v0.2 ships a fixed list of commonly relevant
# kinds" choice rather than the full ``api-resources`` discovery (which would
# cost N round-trips per ``ls`` and risk RBAC-shaped 403 spam on operator
# sessions). The tuple stays fixed at module-scope so the wire shape doesn't
# drift across ``k8s.ls`` calls within a process; T4/T5 will expand it as
# new per-kind ``list`` ops register through the dispatcher.
# ---------------------------------------------------------------------------


#: Tuple of ``(kind_label, CoreV1Api method_name)`` pairs the
#: ``k8s.ls /<namespace>`` handler probes for a per-kind count. The
#: ``method_name`` form is preferred over a bound-callable so the helper
#: doesn't pin a class import at module-load time -- the handler resolves
#: the attr on the freshly-built ``CoreV1Api`` instance at call time, so
#: the registration path in ``ops.py`` stays lightweight.
#:
#: Kept under :class:`kubernetes_asyncio.client.CoreV1Api` so the
#: counting path is a single API-client construction; pods / services /
#: configmaps / events / secrets are all CoreV1 surfaces. ``deployments``
#: and ``ingresses`` live on ``AppsV1Api`` / ``NetworkingV1Api`` and ship
#: with the kind-specific ``list`` ops in T3/T4 -- their count is
#: deferred to those Tasks and surfaces here as a ``not_counted_yet``
#: entry so the operator sees the kind exists without an inaccurate zero.
K8S_NAMESPACED_KIND_LISTERS: tuple[tuple[str, str], ...] = (
    ("pods", "list_namespaced_pod"),
    ("services", "list_namespaced_service"),
    ("configmaps", "list_namespaced_config_map"),
    ("events", "list_namespaced_event"),
    ("persistentvolumeclaims", "list_namespaced_persistent_volume_claim"),
)


#: Cluster-scoped kinds the ``k8s.ls /`` handler advertises as the
#: discoverable surface from the root. These are *names*, not API call
#: shapes -- the agent uses them to pick a follow-up op (``k8s.node.list``,
#: a future ``k8s.persistentvolume.list``, etc.).
K8S_CLUSTER_KINDS: tuple[str, ...] = (
    "nodes",
    "namespaces",
    "persistentvolumes",
    "storageclasses",
)


# ---------------------------------------------------------------------------
# Op metadata -- the per-op KubernetesOp rows that ``ops.py`` re-exports
# through :data:`KUBERNETES_OPS`. Kept in this module rather than appended
# to ``ops.py`` so the T2 surface lives next to its helpers; the connector
# just walks the merged tuple at registration time.
# ---------------------------------------------------------------------------


#: ``k8s.ls`` accepts an optional ``path`` parameter. The semantics:
#: empty / "/" -> cluster root; "/<ns>" -> namespace summary;
#: "/<ns>/<kind>" -> forward to ``k8s.<kind>.list``. The pattern is
#: deliberately permissive in v0.2 -- the dispatcher's JSON Schema layer
#: catches obviously bad shapes, but the handler does the structural
#: parse so it can return a useful ``rows=[]`` for an empty path rather
#: than an opaque schema-violation message.
K8S_LS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Optional inventory path -- '/' for the cluster root, "
                "'/<namespace>' for the per-namespace kind summary, "
                "'/<namespace>/<kind>' to forward to that kind's list "
                "op. Defaults to '/' when omitted."
            ),
            "default": "/",
        },
    },
    "additionalProperties": False,
}


K8S_LS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to discover what's in a Kubernetes cluster without "
        "knowing the right kind-specific op ahead of time. 'k8s.ls /' "
        "lists namespaces + cluster-scoped kinds; 'k8s.ls /<ns>' "
        "shows the resource-kind populations of that namespace; "
        "'k8s.ls /<ns>/<kind>' forwards to k8s.<kind>.list. Use as "
        "the entry point when the operator's question is "
        "exploratory ('what's in argocd?', 'is there anything in "
        "kube-system?')."
    ),
    "parameter_hints": {
        "path": (
            "Optional. Slash-prefixed logical path. Examples: '/', "
            "'/argocd', '/argocd/pods'. Defaults to '/' when omitted."
        ),
    },
    "output_shape": (
        "Three shapes by path: (a) root -> {namespaces: [...], "
        "cluster_kinds: [...]}; (b) namespace -> {namespace: '<ns>', "
        "kinds: [{kind, count}], cluster_kinds_omitted: true}; "
        "(c) namespace/kind -> {forwarded_to: 'k8s.<kind>.list', "
        "result: <OperationResult-as-dict from the sub-dispatch>}."
    ),
}


_K8S_LS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Variant shape -- see llm_instructions.output_shape. The keys "
        "present depend on whether path is the cluster root, a namespace, "
        "or a namespace/kind."
    ),
    "additionalProperties": True,
}


_K8S_NAMESPACE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_K8S_NAMESPACE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                    "age_seconds": {"type": ["integer", "null"]},
                    "labels": {"type": "object"},
                },
                "required": ["name", "status", "age_seconds", "labels"],
                "additionalProperties": False,
            },
            "description": (
                "One row per Kubernetes namespace. Order follows the "
                "API server's response order (typically resource-version "
                "ascending)."
            ),
        },
        "total": {
            "type": "integer",
            "description": (
                "Row count emitted in ``rows``. Useful as the "
                "pre-reduction count -- the dispatcher's default "
                "JsonFluxReducer tracks both the inlined sample size "
                "and this total."
            ),
        },
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_NAMESPACE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what namespaces exist?' or needs "
        "to confirm a namespace is alive before issuing a per-namespace "
        "op. Read-only; safe against any cluster."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{name, status, age_seconds, labels}], 'total': <int>}. "
        "``status`` is the namespace phase string ('Active', "
        "'Terminating'). ``age_seconds`` is integer seconds since the "
        "namespace's ``creation_timestamp``."
    ),
}


_K8S_NODE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_K8S_NODE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "status": {"type": "string"},
                    "roles": {"type": "array", "items": {"type": "string"}},
                    "version": {"type": ["string", "null"]},
                    "kernel": {"type": ["string", "null"]},
                    "os": {"type": ["string", "null"]},
                    "internal_ip": {"type": ["string", "null"]},
                    "taints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": ["string", "null"]},
                                "value": {"type": ["string", "null"]},
                                "effect": {"type": ["string", "null"]},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "age_seconds": {"type": ["integer", "null"]},
                    "labels": {"type": "object"},
                },
                "required": [
                    "name",
                    "status",
                    "roles",
                    "version",
                    "kernel",
                    "os",
                    "internal_ip",
                    "taints",
                    "age_seconds",
                    "labels",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_NODE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call to identify cluster nodes -- 'show me the workers', "
        "'is the control plane healthy?', 'what's the kubelet "
        "version?'. Read-only; pairs with k8s.about for the "
        "control-plane version side of the same question."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{name, status, roles, version, kernel, os, "
        "internal_ip, taints, age_seconds, labels}], 'total': <int>}. "
        "``status`` reflects the Ready condition ('Ready' / 'NotReady' "
        "/ 'Unknown'). ``roles`` is the sorted list of role suffixes "
        "from the node-role.kubernetes.io/<role> labels."
    ),
}


CORE_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.ls",
        handler_attr="k8s_ls",
        summary="List the inventory at a logical path -- root / namespace / kind.",
        description=(
            "Synthetic walker over Kubernetes resources, mirroring "
            "``govc ls /``. With no path or '/', returns the cluster "
            "root view: namespace names and a fixed list of cluster-"
            "scoped kinds. With '/<namespace>', returns a kind -> count "
            "summary for the namespace, querying a fixed list of commonly-"
            "relevant kinds (pods, services, configmaps, events, "
            "persistentvolumeclaims). With '/<namespace>/<kind>', "
            "forwards to k8s.<kind>.list for that namespace by "
            "dispatching the kind-specific op through the connector's "
            "dispatcher shim; kinds whose ``list`` op hasn't shipped yet "
            "(T3/T4 batches) come back through the shim's structured "
            "unknown_op envelope. The handler does not pre-fetch every "
            "row of every kind -- it queries with ``limit=1`` and reads "
            "``remaining_item_count`` from the list metadata so an "
            "``ls`` against a busy namespace stays cheap."
        ),
        parameter_schema=K8S_LS_PARAMETER_SCHEMA,
        response_schema=_K8S_LS_RESPONSE_SCHEMA,
        group_key="inventory",
        tags=("read-only", "inventory", "discovery"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_LS_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.namespace.list",
        handler_attr="k8s_namespace_list",
        summary="List Kubernetes namespaces with phase, age, and labels.",
        description=(
            "Calls ``CoreV1Api.list_namespace()`` and projects each "
            "row down to {name, status, age_seconds, labels}. ``status`` "
            "is the namespace phase string the API server returns "
            "('Active' / 'Terminating'). Read-only; pairs with ``k8s.ls "
            "/`` for the operator-facing 'what's in this cluster?' "
            "question -- the namespace.list form returns full per-row "
            "detail, ls returns just the names."
        ),
        parameter_schema=_K8S_NAMESPACE_LIST_PARAMETER_SCHEMA,
        response_schema=_K8S_NAMESPACE_LIST_RESPONSE_SCHEMA,
        group_key="inventory",
        tags=("read-only", "inventory", "namespace"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_NAMESPACE_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.node.list",
        handler_attr="k8s_node_list",
        summary="List Kubernetes nodes with status, roles, version, taints.",
        description=(
            "Calls ``CoreV1Api.list_node()`` and projects each row "
            "down to {name, status, roles, version, kernel, os, "
            "internal_ip, taints, age_seconds, labels}. ``status`` "
            "reflects the Ready condition: 'Ready' when the Ready "
            "condition's status is 'True', 'NotReady' on any other "
            "state, 'Unknown' when the condition is missing. ``roles`` "
            "is derived from the node's "
            "``node-role.kubernetes.io/<role>`` label keys (sorted). "
            "Read-only."
        ),
        parameter_schema=_K8S_NODE_LIST_PARAMETER_SCHEMA,
        response_schema=_K8S_NODE_LIST_RESPONSE_SCHEMA,
        group_key="inventory",
        tags=("read-only", "inventory", "node"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_NODE_LIST_LLM_INSTRUCTIONS,
    ),
)
