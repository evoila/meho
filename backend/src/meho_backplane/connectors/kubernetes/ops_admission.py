# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admission webhook read -- ``k8s.admission_webhook.list`` (#3840).

An admission webhook can refuse or change an object while the API server
handles a write. So "why did this apply not take effect?" often means
"which webhook saw this request?". This op lists every webhook of every
``ValidatingWebhookConfiguration`` and ``MutatingWebhookConfiguration``
(``AdmissionregistrationV1Api.list_validating_webhook_configuration`` /
``list_mutating_webhook_configuration``), one row per webhook, with the
fields that decide when it runs and what happens when it fails.

The ``caBundle`` is left out: it is long, it is not needed to diagnose
an apply, and the row stays small.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_job import label_selector_string

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = ["ADMISSION_OPS", "admission_webhook_rows", "k8s_admission_webhook_list"]


def _service_ref(config: Any) -> dict[str, Any] | None:
    service = config.service if config is not None else None
    if service is None:
        return None
    return {
        "namespace": service.namespace,
        "name": service.name,
        "path": service.path,
        "port": service.port,
    }


def _rule_row(rule: Any) -> dict[str, Any]:
    return {
        "operations": list(rule.operations or []),
        "api_groups": list(rule.api_groups or []),
        "api_versions": list(rule.api_versions or []),
        "resources": list(rule.resources or []),
        "scope": rule.scope,
    }


def _webhook_row(configuration: str | None, webhook_type: str, webhook: Any) -> dict[str, Any]:
    config = webhook.client_config
    return {
        "configuration": configuration,
        "type": webhook_type,
        "name": webhook.name,
        "failure_policy": webhook.failure_policy,
        "match_policy": webhook.match_policy,
        "side_effects": webhook.side_effects,
        "timeout_seconds": webhook.timeout_seconds,
        "reinvocation_policy": getattr(webhook, "reinvocation_policy", None),
        "service": _service_ref(config),
        "url": config.url if config is not None else None,
        "rules": [_rule_row(rule) for rule in webhook.rules or []],
        "namespace_selector": label_selector_string(webhook.namespace_selector),
        "object_selector": label_selector_string(webhook.object_selector),
        "match_conditions": [
            {"name": cond.name, "expression": cond.expression}
            for cond in webhook.match_conditions or []
        ],
    }


def admission_webhook_rows(configurations: list[Any], webhook_type: str) -> list[dict[str, Any]]:
    """Flatten webhook configurations into one row per webhook.

    *webhook_type* is ``validating`` or ``mutating``. A configuration
    with no webhooks adds no row.
    """
    rows: list[dict[str, Any]] = []
    for item in configurations:
        name = item.metadata.name if item.metadata is not None else None
        rows.extend(_webhook_row(name, webhook_type, hook) for hook in item.webhooks or [])
    return rows


async def k8s_admission_webhook_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.admission_webhook.list``."""
    webhook_type: str = params.get("type", "all")
    api_client = await connector._get_api_client(target, operator)
    admission_v1 = client.AdmissionregistrationV1Api(api_client)
    rows: list[dict[str, Any]] = []
    if webhook_type in ("all", "validating"):
        validating = await admission_v1.list_validating_webhook_configuration()
        rows.extend(admission_webhook_rows(validating.items or [], "validating"))
    if webhook_type in ("all", "mutating"):
        mutating = await admission_v1.list_mutating_webhook_configuration()
        rows.extend(admission_webhook_rows(mutating.items or [], "mutating"))
    return {"rows": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# Parameter schema, response schema, LLM instructions, registration
# ---------------------------------------------------------------------------


K8S_ADMISSION_WEBHOOK_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["all", "validating", "mutating"],
            "default": "all",
            "description": "Which webhooks to list. Default: all.",
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"type": "validating"}],
}


_NULLABLE_STR = {"type": ["string", "null"]}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

K8S_ADMISSION_WEBHOOK_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "configuration": _NULLABLE_STR,
                    "type": {"type": "string", "enum": ["validating", "mutating"]},
                    "name": _NULLABLE_STR,
                    "failure_policy": _NULLABLE_STR,
                    "match_policy": _NULLABLE_STR,
                    "side_effects": _NULLABLE_STR,
                    "timeout_seconds": {"type": ["integer", "null"]},
                    "reinvocation_policy": _NULLABLE_STR,
                    "service": {"type": ["object", "null"]},
                    "url": _NULLABLE_STR,
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "operations": _STR_LIST,
                                "api_groups": _STR_LIST,
                                "api_versions": _STR_LIST,
                                "resources": _STR_LIST,
                                "scope": _NULLABLE_STR,
                            },
                            "additionalProperties": False,
                        },
                    },
                    "namespace_selector": _NULLABLE_STR,
                    "object_selector": _NULLABLE_STR,
                    "match_conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": _NULLABLE_STR, "expression": _NULLABLE_STR},
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["configuration", "type", "name", "failure_policy", "rules"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


K8S_ADMISSION_WEBHOOK_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call when an apply was refused, changed, or timed out and the "
        "reason may be an admission webhook: 'which webhooks see a create "
        "of certificates?', 'which webhook has failurePolicy Fail?'. Lists "
        "the validating and mutating webhooks of the cluster. Read-only; "
        "cluster-scoped."
    ),
    "parameter_hints": {"type": "all (default), validating or mutating."},
    "output_shape": (
        "{'rows': [{configuration, type, name, failure_policy, match_policy, "
        "side_effects, timeout_seconds, reinvocation_policy, service, url, "
        "rules: [{operations, api_groups, api_versions, resources, scope}], "
        "namespace_selector, object_selector, match_conditions}], 'total'}. "
        "A webhook runs for a request when one of its rules matches the "
        "operation and resource and both selectors match. failure_policy "
        "Fail means the request is refused when the webhook cannot be "
        "reached. caBundle is never returned."
    ),
    "next_step": (
        "If a webhook's service is down, check its pods with k8s.pod.list in "
        "the service namespace. For the identity's own permissions use "
        "k8s.auth.can_i."
    ),
}


ADMISSION_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.admission_webhook.list",
        handler_attr="k8s_admission_webhook_list",
        summary="List admission webhooks -- rules, selectors, failure policy.",
        description=(
            "Calls AdmissionregistrationV1Api list_validating_webhook_configuration "
            "and list_mutating_webhook_configuration. One row per webhook with its "
            "rules, selectors, failure policy, timeout and service or URL. "
            "caBundle is left out. Cluster-scoped; read-only."
        ),
        parameter_schema=K8S_ADMISSION_WEBHOOK_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_ADMISSION_WEBHOOK_LIST_RESPONSE_SCHEMA,
        group_key="access",
        tags=("read-only", "admission", "webhook"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_ADMISSION_WEBHOOK_LIST_LLM_INSTRUCTIONS,
    ),
)
