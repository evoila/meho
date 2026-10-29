# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Access checks -- ``k8s.auth.can_i`` / ``k8s.auth.rules`` (#3840).

When an apply fails or does nothing, the first question is often "may
the identity MEHO uses on this cluster do this at all?". These two ops
ask the API server, the same way ``kubectl auth can-i`` does:

* ``k8s.auth.can_i`` sends a ``SelfSubjectAccessReview``
  (``AuthorizationV1Api.create_self_subject_access_review``) for one
  verb on one resource (or one non-resource path such as ``/healthz``)
  and returns ``allowed`` / ``denied`` / ``reason``.
* ``k8s.auth.rules`` sends a ``SelfSubjectRulesReview``
  (``AuthorizationV1Api.create_self_subject_rules_review``) for one
  namespace and returns the rules the identity has there, one row per
  rule (``kubectl auth can-i --list``).

Both are HTTP ``POST`` calls, but the API server stores nothing: a
review is answered and dropped. So both ops are ``safe`` reads. The
identity checked is always the one in the target's kubeconfig (the
identity MEHO uses for every other op on that target), not the person
or agent calling MEHO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "ACCESS_OPS",
    "access_review_body",
    "access_review_result",
    "k8s_auth_can_i",
    "k8s_auth_rules",
    "rules_review_result",
]


_RESOURCE_FIELDS = ("group", "version", "resource", "subresource", "name", "namespace")


def access_review_body(params: dict[str, Any]) -> client.V1SelfSubjectAccessReview:
    """Build the ``SelfSubjectAccessReview`` for *params* (resource or non-resource form)."""
    verb: str = params["verb"]
    path = params.get("non_resource_path")
    if path:
        spec = client.V1SelfSubjectAccessReviewSpec(
            non_resource_attributes=client.V1NonResourceAttributes(path=path, verb=verb)
        )
    else:
        attrs = {key: params[key] for key in _RESOURCE_FIELDS if params.get(key) is not None}
        spec = client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=client.V1ResourceAttributes(verb=verb, **attrs)
        )
    return client.V1SelfSubjectAccessReview(
        api_version="authorization.k8s.io/v1", kind="SelfSubjectAccessReview", spec=spec
    )


def access_review_result(
    review: client.V1SelfSubjectAccessReview, params: dict[str, Any]
) -> dict[str, Any]:
    """Project the answered review into ``{allowed, denied, reason, evaluation_error, ...}``."""
    status = review.status
    return {
        "allowed": bool(status.allowed) if status is not None else False,
        "denied": bool(status.denied) if status is not None else False,
        "reason": (status.reason or None) if status is not None else None,
        "evaluation_error": (status.evaluation_error or None) if status is not None else None,
        "verb": params["verb"],
        "resource": params.get("resource"),
        "group": params.get("group"),
        "subresource": params.get("subresource"),
        "name": params.get("name"),
        "namespace": params.get("namespace"),
        "non_resource_path": params.get("non_resource_path"),
    }


def rules_review_result(review: client.V1SelfSubjectRulesReview, namespace: str) -> dict[str, Any]:
    """Project the answered rules review into ``{rows, total, incomplete, evaluation_error}``.

    One row per rule. A resource rule fills ``api_groups`` / ``resources``
    / ``resource_names``; a non-resource rule fills ``non_resource_urls``.
    """
    status = review.status
    rows: list[dict[str, Any]] = []
    if status is not None:
        for rule in status.resource_rules or []:
            rows.append(
                {
                    "kind": "resource",
                    "verbs": list(rule.verbs or []),
                    "api_groups": list(rule.api_groups or []),
                    "resources": list(rule.resources or []),
                    "resource_names": list(rule.resource_names or []),
                    "non_resource_urls": [],
                }
            )
        for nr_rule in status.non_resource_rules or []:
            rows.append(
                {
                    "kind": "non_resource",
                    "verbs": list(nr_rule.verbs or []),
                    "api_groups": [],
                    "resources": [],
                    "resource_names": [],
                    "non_resource_urls": list(nr_rule.non_resource_urls or []),
                }
            )
    return {
        "namespace": namespace,
        "rows": rows,
        "total": len(rows),
        "incomplete": bool(status.incomplete) if status is not None else False,
        "evaluation_error": (status.evaluation_error or None) if status is not None else None,
    }


async def k8s_auth_can_i(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.auth.can_i``: one ``SelfSubjectAccessReview``."""
    api_client = await connector._get_api_client(target, operator)
    auth_v1 = client.AuthorizationV1Api(api_client)
    review = await auth_v1.create_self_subject_access_review(body=access_review_body(params))
    return access_review_result(review, params)


async def k8s_auth_rules(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.auth.rules``: one ``SelfSubjectRulesReview`` for a namespace."""
    namespace: str = params["namespace"]
    api_client = await connector._get_api_client(target, operator)
    auth_v1 = client.AuthorizationV1Api(api_client)
    body = client.V1SelfSubjectRulesReview(
        api_version="authorization.k8s.io/v1",
        kind="SelfSubjectRulesReview",
        spec=client.V1SelfSubjectRulesReviewSpec(namespace=namespace),
    )
    review = await auth_v1.create_self_subject_rules_review(body=body)
    return rules_review_result(review, namespace)


# ---------------------------------------------------------------------------
# Parameter schemas, response schemas, LLM instructions, registration
# ---------------------------------------------------------------------------


def _str_param(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "description": description}


K8S_AUTH_CAN_I_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verb": _str_param(
            "The action: get, list, watch, create, update, patch, delete, "
            "deletecollection, or '*' for any. For a non-resource path use "
            "the HTTP verb in lower case (get, post)."
        ),
        "resource": _str_param("Plural resource name, e.g. deployments, secrets, certificates."),
        "group": {
            "type": "string",
            "description": "API group of the resource, e.g. apps. Empty or omitted = core group.",
        },
        "version": _str_param("API version. Usually omitted (any version)."),
        "subresource": _str_param("Sub-resource, e.g. status, scale, log."),
        "name": _str_param("One object name. Omit to ask about all objects."),
        "namespace": _str_param("Namespace. Omit for a cluster-scoped resource or all namespaces."),
        "non_resource_path": {
            "type": "string",
            "pattern": "^/",
            "description": "A non-resource URL path such as /healthz or /metrics.",
        },
    },
    "required": ["verb"],
    "oneOf": [
        {"required": ["resource"], "not": {"required": ["non_resource_path"]}},
        {"required": ["non_resource_path"], "not": {"required": ["resource"]}},
    ],
    "additionalProperties": False,
    "examples": [
        {"verb": "patch", "group": "apps", "resource": "deployments", "namespace": "shop"},
        {"verb": "create", "group": "cert-manager.io", "resource": "certificates"},
        {"verb": "get", "non_resource_path": "/healthz"},
    ],
}


_NULLABLE_STR = {"type": ["string", "null"]}

K8S_AUTH_CAN_I_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "allowed": {"type": "boolean"},
        "denied": {"type": "boolean"},
        "reason": _NULLABLE_STR,
        "evaluation_error": _NULLABLE_STR,
        "verb": {"type": "string"},
        "resource": _NULLABLE_STR,
        "group": _NULLABLE_STR,
        "subresource": _NULLABLE_STR,
        "name": _NULLABLE_STR,
        "namespace": _NULLABLE_STR,
        "non_resource_path": _NULLABLE_STR,
    },
    "required": ["allowed", "denied", "reason", "evaluation_error", "verb"],
    "additionalProperties": False,
}


K8S_AUTH_CAN_I_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to check whether MEHO's identity on this cluster may do one "
        "action: 'can MEHO patch deployments in shop?', 'why was this "
        "apply refused?'. The same as 'kubectl auth can-i'. Read-only: the "
        "API server answers the question and stores nothing. It checks the "
        "identity in the target's kubeconfig, not the caller."
    ),
    "parameter_hints": {
        "verb": "get / list / create / patch / delete / ... or '*'.",
        "resource": "Plural name, e.g. deployments. Use this OR non_resource_path.",
        "group": "API group, e.g. apps. Omit for the core group (pods, secrets).",
        "namespace": "Omit for cluster-scoped resources or to ask about all namespaces.",
        "non_resource_path": "e.g. /healthz. Use this OR resource.",
    },
    "output_shape": (
        "{allowed, denied, reason, evaluation_error, ...the question asked}. "
        "allowed=false with denied=false means no rule allows it (the "
        "usual 'no'). denied=true means a rule forbids it outright."
    ),
    "next_step": (
        "To see everything the identity may do in a namespace, call "
        "k8s.auth.rules. If the action is allowed but the apply still "
        "failed, look at k8s.admission_webhook.list and k8s.event.list."
    ),
}


K8S_AUTH_RULES_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": _str_param(
            "Namespace to list the rules for. Cluster-wide rules are included."
        ),
    },
    "required": ["namespace"],
    "additionalProperties": False,
    "examples": [{"namespace": "default"}],
}


_STR_LIST = {"type": "array", "items": {"type": "string"}}

K8S_AUTH_RULES_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["resource", "non_resource"]},
                    "verbs": _STR_LIST,
                    "api_groups": _STR_LIST,
                    "resources": _STR_LIST,
                    "resource_names": _STR_LIST,
                    "non_resource_urls": _STR_LIST,
                },
                "required": [
                    "kind",
                    "verbs",
                    "api_groups",
                    "resources",
                    "resource_names",
                    "non_resource_urls",
                ],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
        "incomplete": {"type": "boolean"},
        "evaluation_error": _NULLABLE_STR,
    },
    "required": ["namespace", "rows", "total", "incomplete", "evaluation_error"],
    "additionalProperties": False,
}


K8S_AUTH_RULES_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to list everything MEHO's identity on this cluster may do in "
        "one namespace: 'what can MEHO do in shop?'. The same as 'kubectl "
        "auth can-i --list -n <ns>'. Read-only. For one yes/no question "
        "use k8s.auth.can_i instead."
    ),
    "parameter_hints": {"namespace": "Required. Cluster-wide rules are part of the answer."},
    "output_shape": (
        "{namespace, rows: [{kind, verbs, api_groups, resources, "
        "resource_names, non_resource_urls}], total, incomplete, "
        "evaluation_error}. incomplete=true means some authorizers cannot "
        "list their rules, so the list may miss permissions -- confirm a "
        "single action with k8s.auth.can_i."
    ),
    "next_step": (
        "If a rule you need is missing, confirm it with k8s.auth.can_i. If the "
        "rule is there but the apply still failed, look at "
        "k8s.admission_webhook.list and k8s.event.list."
    ),
}


ACCESS_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.auth.can_i",
        handler_attr="k8s_auth_can_i",
        summary="Check if MEHO's identity on the cluster may do one action (kubectl auth can-i).",
        description=(
            "Sends a SelfSubjectAccessReview "
            "(AuthorizationV1Api.create_self_subject_access_review) for one verb "
            "on one resource or one non-resource path. Returns {allowed, denied, "
            "reason, evaluation_error}. The API server stores nothing."
        ),
        parameter_schema=K8S_AUTH_CAN_I_PARAMETER_SCHEMA,
        response_schema=K8S_AUTH_CAN_I_RESPONSE_SCHEMA,
        group_key="access",
        tags=("read-only", "rbac", "access-review"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_AUTH_CAN_I_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.auth.rules",
        handler_attr="k8s_auth_rules",
        summary="List what MEHO's identity may do in one namespace (kubectl auth can-i --list).",
        description=(
            "Sends a SelfSubjectRulesReview with "
            "AuthorizationV1Api.create_self_subject_rules_review for one "
            "namespace. One row per rule: {kind, verbs, api_groups, resources, "
            "resource_names, non_resource_urls}, plus the 'incomplete' flag and "
            "any evaluation error. The API server stores nothing."
        ),
        parameter_schema=K8S_AUTH_RULES_PARAMETER_SCHEMA,
        response_schema=K8S_AUTH_RULES_RESPONSE_SCHEMA,
        group_key="access",
        tags=("read-only", "rbac", "access-review"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_AUTH_RULES_LLM_INSTRUCTIONS,
    ),
)
