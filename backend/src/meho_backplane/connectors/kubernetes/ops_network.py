# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network ops -- ``k8s.service.list`` / ``k8s.ingress.list``.

G3.2-T4 (#324) of Initiative #320. Adds the two "what's exposed?"
read-only ops on top of the T1 / T2 / T5 base substrate. G0.17-T1
(#1330) converged the request shape onto the workload list ops' base
(``namespace`` XOR ``all_namespaces`` + ``label_selector``) so the
operator's "what argocd-labeled services exist cluster-wide?" question
maps to a single ``{all_namespaces: true, label_selector: '...'}``
call instead of an N-namespace client-side loop.

* ``k8s.service.list [--namespace X | --all-namespaces]
  [--label-selector ...]`` -- ``CoreV1Api.list_namespaced_service``
  (per-namespace) / ``CoreV1Api.list_service_for_all_namespaces``
  (cluster-wide). Returns one row per Service with name / namespace /
  type / cluster_ip / external_ips / ports / selector. The ``ports``
  projection flattens :class:`V1ServicePort` to the operator-visible
  four-tuple (``name`` / ``port`` / ``target_port`` / ``protocol``).
* ``k8s.ingress.list [--namespace X | --all-namespaces]
  [--label-selector ...]`` --
  ``NetworkingV1Api.list_namespaced_ingress`` /
  ``NetworkingV1Api.list_ingress_for_all_namespaces``. Returns one row
  per Ingress with name / namespace / class / hosts / tls_hosts /
  rules. The ``hosts`` list deduplicates entries across rules (the
  spec allows the same host on multiple rules with different path
  sets); ``tls_hosts`` is the union of every ``V1IngressTLS.hosts``
  entry so the operator can spot whether the ingress carries a TLS
  certificate without walking the full rule tree.

Server-side ``limit`` / ``_continue`` paging is **deliberately not
exposed** for these two ops in G0.17-T1 -- service / ingress
populations are typically O(10) per namespace and the issue's
acceptance section calls out the paging widening as a follow-up
candidate. The schema's XOR + label_selector knob is the cross-namespace
fix Finding 24 motivates; the paging widening is mechanical and can
land as a sibling task if a future deployment hits a heavy-tenancy
namespace.

Row-shape helpers (:func:`service_row`, :func:`service_port_row`,
:func:`ingress_row`, :func:`ingress_rule_row`) are pure functions over
:mod:`kubernetes_asyncio.client.models` instances so the unit tests can
pin the wire shape against synthetic fixtures without booting an event
loop -- the same discipline the T2 ops_core helpers established.

The handlers themselves live as bound methods on
:class:`~meho_backplane.connectors.kubernetes.connector.KubernetesConnector`
(``k8s_service_list``, ``k8s_ingress_list``) and delegate the model ->
row projection to the helpers here.

References
----------
* Parent task: G3.2-T4 (#324); request-shape parity: G0.17-T1 (#1330).
* Parent Initiative: G3.2 (#320), kubernetes-asyncio typed connector.
* Conventions doc: ``docs/codebase/api-shape-conventions.md`` §10
  (intra-connector list-op request-shape parity).
* k8s Service API: https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
* k8s Ingress API: https://kubernetes.io/docs/reference/kubernetes-api/service-resources/ingress-v1/
* ``kubernetes_asyncio.CoreV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
* ``kubernetes_asyncio.NetworkingV1Api``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/NetworkingV1Api.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_listparams import (
    ALL_NAMESPACES_PARAM,
    LABEL_SELECTOR_PARAM,
    NAMESPACE_PARAM,
    NAMESPACE_XOR_ALL_NAMESPACES,
)

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import (
        V1HTTPIngressPath,
        V1Ingress,
        V1IngressRule,
        V1Service,
        V1ServicePort,
    )

__all__ = [
    "K8S_INGRESS_LIST_LLM_INSTRUCTIONS",
    "K8S_INGRESS_LIST_PARAMETER_SCHEMA",
    "K8S_INGRESS_LIST_RESPONSE_SCHEMA",
    "K8S_SERVICE_LIST_LLM_INSTRUCTIONS",
    "K8S_SERVICE_LIST_PARAMETER_SCHEMA",
    "K8S_SERVICE_LIST_RESPONSE_SCHEMA",
    "NETWORK_OPS",
    "ingress_path_row",
    "ingress_row",
    "ingress_rule_row",
    "service_port_row",
    "service_row",
]


# ---------------------------------------------------------------------------
# Row-shape helpers -- pure mappings over kubernetes_asyncio model objects.
# ---------------------------------------------------------------------------


def service_port_row(port: V1ServicePort) -> dict[str, Any]:
    """Project a :class:`V1ServicePort` into the operator-visible four-tuple.

    ``target_port`` is the operator-supplied port on the destination
    pod; the K8s schema permits either an integer or a named string
    reference. The wire shape forwards the value verbatim so a port
    named ``"http"`` in the manifest surfaces as ``"http"`` on the row,
    not coerced to an integer.
    """
    return {
        "name": port.name,
        "port": port.port,
        "target_port": port.target_port,
        "protocol": port.protocol,
    }


def service_row(svc: V1Service) -> dict[str, Any]:
    """Project a :class:`V1Service` into the wire dict shape.

    ``external_ips`` is the static list operators set on
    ``spec.externalIPs``; LoadBalancer-assigned IPs live under
    ``status.loadBalancer.ingress`` and are out of v0.2 scope (the
    operator question "what IP routes to this service?" is answered by
    the type + cluster_ip combination plus the namespace's Ingress
    rows). ``selector`` is forwarded verbatim; an absent selector
    (``None`` on a headless / ExternalName service) surfaces as ``{}``
    so downstream consumers see a stable dict type.
    """
    metadata = svc.metadata
    spec = svc.spec
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "type": spec.type if spec is not None else None,
        "cluster_ip": spec.cluster_ip if spec is not None else None,
        "external_ips": (list(spec.external_ips or []) if spec is not None else []),
        "ports": ([service_port_row(p) for p in (spec.ports or [])] if spec is not None else []),
        "selector": (dict(spec.selector or {}) if spec is not None else {}),
    }


def ingress_path_row(path: V1HTTPIngressPath) -> dict[str, Any]:
    """Project a :class:`V1HTTPIngressPath` into the wire dict shape.

    ``service`` / ``port`` collapse the nested ``backend.service``
    structure to two flat fields -- operators reading the row don't
    care about the IngressBackend wrapper, only "which service +
    port does this path route to?". When the backend is a
    ``Resource`` reference (non-Service backend), both fields surface
    as ``None`` and the operator can re-fetch the full ingress via
    a future ``k8s.ingress.info`` op (out of v0.2 scope).
    """
    service_name: str | None = None
    service_port: int | str | None = None
    backend = path.backend
    if backend is not None and backend.service is not None:
        service_name = backend.service.name
        port_obj = backend.service.port
        if port_obj is not None:
            # ``V1ServiceBackendPort`` has either ``number`` (int) or
            # ``name`` (string); prefer ``number`` because the operator
            # mental model maps to a TCP port. A named port falls back
            # to the string -- still useful, just less common.
            service_port = port_obj.number if port_obj.number is not None else port_obj.name
    return {
        "path": path.path,
        "path_type": path.path_type,
        "service": service_name,
        "port": service_port,
    }


def ingress_rule_row(rule: V1IngressRule) -> dict[str, Any]:
    """Project a :class:`V1IngressRule` into the wire dict shape.

    The HTTP rule is the only path-bearing branch in v0.2; non-HTTP
    rules (TCP / UDP via Gateway API) are out of scope per #320.
    """
    paths: list[dict[str, Any]] = []
    if rule.http is not None and rule.http.paths:
        paths = [ingress_path_row(p) for p in rule.http.paths]
    return {
        "host": rule.host,
        "paths": paths,
    }


def ingress_row(ingress: V1Ingress) -> dict[str, Any]:
    """Project a :class:`V1Ingress` into the wire dict shape.

    ``hosts`` is the deduplicated union of every rule's ``host`` field
    (the spec permits the same host on multiple rules with disjoint
    path sets). ``tls_hosts`` is the union of every TLS entry's
    ``hosts`` list. Both lists are sorted for stable wire output across
    the API's iteration order.
    """
    metadata = ingress.metadata
    spec = ingress.spec
    ingress_class: str | None = None
    rules: list[dict[str, Any]] = []
    hosts: set[str] = set()
    tls_hosts: set[str] = set()
    if spec is not None:
        ingress_class = spec.ingress_class_name
        if spec.rules:
            rules = [ingress_rule_row(r) for r in spec.rules]
            for rule in spec.rules:
                if rule.host:
                    hosts.add(rule.host)
        if spec.tls:
            for tls in spec.tls:
                if tls.hosts:
                    for host in tls.hosts:
                        tls_hosts.add(host)
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "class": ingress_class,
        "hosts": sorted(hosts),
        "tls_hosts": sorted(tls_hosts),
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# Op metadata -- schemas + llm_instructions + KubernetesOp rows.
# ---------------------------------------------------------------------------


#: ``k8s.service.list`` parameter schema. Adopts the shared
#: ``namespace`` XOR ``all_namespaces`` selector + ``label_selector``
#: (G0.17-T1 #1330). Server-side paging (``limit``/``continue_token``)
#: is deliberately omitted -- see the module docstring.
K8S_SERVICE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": NAMESPACE_PARAM,
        "all_namespaces": ALL_NAMESPACES_PARAM,
        "label_selector": LABEL_SELECTOR_PARAM,
    },
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
}


K8S_SERVICE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "namespace": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "cluster_ip": {"type": ["string", "null"]},
                    "external_ips": {"type": "array", "items": {"type": "string"}},
                    "ports": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": ["string", "null"]},
                                "port": {"type": ["integer", "null"]},
                                "target_port": {"type": ["integer", "string", "null"]},
                                "protocol": {"type": ["string", "null"]},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "selector": {"type": "object"},
                },
                "required": [
                    "name",
                    "namespace",
                    "type",
                    "cluster_ip",
                    "external_ips",
                    "ports",
                    "selector",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_SERVICE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what services are in "
        "<namespace>?', 'what's exposing the argocd UI?', or needs the "
        "cluster-internal addresses (ClusterIP + port) for a service. "
        "Use ``all_namespaces=true`` for cluster-wide listings (e.g. "
        "'what argocd-labeled services exist across the cluster?'). "
        "Read-only; safe."
    ),
    "parameter_hints": {
        "namespace": "Required unless ``all_namespaces`` is true.",
        "all_namespaces": (
            "Pass true for cluster-wide listings; mutually exclusive with ``namespace``."
        ),
        "label_selector": (
            "Optional. k8s label-selector syntax (e.g. "
            "``app=argocd-server``, ``role in (cp,etcd)``)."
        ),
    },
    "output_shape": (
        "{'rows': [{name, namespace, type, cluster_ip, external_ips, "
        "ports: [{name, port, target_port, protocol}], selector}], "
        "'total': <int>}. ``type`` is the Service type "
        "('ClusterIP' / 'NodePort' / 'LoadBalancer' / 'ExternalName'); "
        "``cluster_ip`` is the stable in-cluster VIP. Each row "
        "carries its own ``namespace`` so cross-namespace rows under "
        "``all_namespaces=true`` stay distinguishable."
    ),
}


K8S_INGRESS_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": NAMESPACE_PARAM,
        "all_namespaces": ALL_NAMESPACES_PARAM,
        "label_selector": LABEL_SELECTOR_PARAM,
    },
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
}


K8S_INGRESS_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "namespace": {"type": ["string", "null"]},
                    "class": {"type": ["string", "null"]},
                    "hosts": {"type": "array", "items": {"type": "string"}},
                    "tls_hosts": {"type": "array", "items": {"type": "string"}},
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "host": {"type": ["string", "null"]},
                                "paths": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "path": {"type": ["string", "null"]},
                                            "path_type": {"type": ["string", "null"]},
                                            "service": {"type": ["string", "null"]},
                                            "port": {"type": ["integer", "string", "null"]},
                                        },
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "name",
                    "namespace",
                    "class",
                    "hosts",
                    "tls_hosts",
                    "rules",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_INGRESS_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what URLs route into "
        "<namespace>?', 'is there a TLS cert on the argocd ingress?', "
        "or needs the host->service routing table. Use "
        "``all_namespaces=true`` for cluster-wide listings (e.g. "
        "'what hostnames does the cluster serve?'). Read-only; safe."
    ),
    "parameter_hints": {
        "namespace": "Required unless ``all_namespaces`` is true.",
        "all_namespaces": (
            "Pass true for cluster-wide listings; mutually exclusive with ``namespace``."
        ),
        "label_selector": ("Optional. k8s label-selector syntax (e.g. ``app=argocd-server``)."),
    },
    "output_shape": (
        "{'rows': [{name, namespace, class, hosts, tls_hosts, "
        "rules: [{host, paths: [{path, path_type, service, port}]}]}], "
        "'total': <int>}. ``hosts`` and ``tls_hosts`` are sorted "
        "deduplicated lists across the rule set. Each row carries "
        "its own ``namespace`` so cross-namespace rows under "
        "``all_namespaces=true`` stay distinguishable."
    ),
}


NETWORK_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.service.list",
        handler_attr="k8s_service_list",
        summary=(
            "List Kubernetes services per-namespace or cluster-wide "
            "-- type / cluster_ip / ports / selector."
        ),
        description=(
            "Calls ``CoreV1Api.list_namespaced_service(namespace, ...)`` "
            "(per-namespace) or "
            "``CoreV1Api.list_service_for_all_namespaces(...)`` "
            "(``all_namespaces=true``) and projects each Service into "
            "{name, namespace, type, cluster_ip, external_ips, ports, "
            "selector}. ``type`` is the Service type ('ClusterIP' / "
            "'NodePort' / 'LoadBalancer' / 'ExternalName'); "
            "``cluster_ip`` is the stable in-cluster VIP (may be 'None' "
            "for ExternalName services or headless services with "
            "selector). ``ports`` flattens each ``V1ServicePort`` to "
            "{name, port, target_port, protocol}; ``target_port`` may "
            "be either an integer or a named-port string. ``selector`` "
            "is the label map the service uses to pick pods. "
            "``label_selector`` is forwarded server-side. Read-only."
        ),
        parameter_schema=K8S_SERVICE_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_SERVICE_LIST_RESPONSE_SCHEMA,
        group_key="network",
        tags=("read-only", "network", "service"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_SERVICE_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.ingress.list",
        handler_attr="k8s_ingress_list",
        summary=(
            "List Kubernetes ingresses per-namespace or cluster-wide "
            "-- hosts / TLS hosts / routing rules."
        ),
        description=(
            "Calls "
            "``NetworkingV1Api.list_namespaced_ingress(namespace, ...)`` "
            "(per-namespace) or "
            "``NetworkingV1Api.list_ingress_for_all_namespaces(...)`` "
            "(``all_namespaces=true``) and projects each Ingress into "
            "{name, namespace, class, hosts, tls_hosts, rules}. "
            "``class`` is the ``ingressClassName`` (e.g. 'nginx', "
            "'traefik'); ``hosts`` is the deduplicated sorted union of "
            "every rule's host; ``tls_hosts`` is the union of every "
            "TLS entry's hosts list (operators use it to spot whether "
            "the ingress has TLS configured without walking the rule "
            "tree). ``rules`` flattens each ``V1IngressRule`` to "
            "{host, paths: [{path, path_type, service, port}]}; the "
            "backend's IngressServiceBackend wrapper is collapsed to "
            "the flat service+port fields for the operator-facing wire "
            "shape. ``label_selector`` is forwarded server-side. "
            "Read-only."
        ),
        parameter_schema=K8S_INGRESS_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_INGRESS_LIST_RESPONSE_SCHEMA,
        group_key="network",
        tags=("read-only", "network", "ingress"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_INGRESS_LIST_LLM_INSTRUCTIONS,
    ),
)
