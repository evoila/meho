# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Declarative metadata for the G3.14-T1 (#1403) K8s write ops.

Holds the JSON-Schema parameter shapes, the LLM-instruction blocks, and
the two :class:`~meho_backplane.connectors.kubernetes.ops.KubernetesOp`
registration tuples for every single-call write op. Split out of the
handler modules (:mod:`ops_write` caution / :mod:`ops_write_dangerous`
dangerous) so each handler file stays under the 600-line code-quality
cap and so the declarative surface (what the operator/agent sees) reads
in one place, independent of the imperative handlers.

The op rows reference handlers by ``handler_attr`` (a string resolved
against :class:`KubernetesConnector` at registration time), so there is
no import-time coupling from this metadata module back to the handler
functions -- only to the kind dispatch tables (for the ``enum`` lists),
which live with their handlers.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_write import ANNOTATABLE_KINDS
from meho_backplane.connectors.kubernetes.ops_write_dangerous import DELETABLE_KINDS

__all__ = ["WRITE_CAUTION_OPS", "WRITE_DANGEROUS_OPS"]


_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Exact object name (no prefix resolution on write ops).",
}
_NAMESPACE_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Namespace the object lives in.",
}


# ---------------------------------------------------------------------------
# Caution-class schemas + LLM instructions
# ---------------------------------------------------------------------------


_SCALE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROP,
        "namespace": _NAMESPACE_PROP,
        "replicas": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000,
            "description": "Desired replica count. 0 scales the Deployment down to nothing.",
        },
    },
    "required": ["name", "namespace", "replicas"],
    "additionalProperties": False,
}


_ROLLOUT_RESTART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": _NAME_PROP, "namespace": _NAMESPACE_PROP},
    "required": ["name", "namespace"],
    "additionalProperties": False,
}


_NAMESPACE_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 63,
            "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
            "description": (
                "DNS-1123-label namespace name. Idempotent: a 409 is reported, not raised."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


_KIND_PROP: dict[str, Any] = {
    "type": "string",
    "enum": sorted(ANNOTATABLE_KINDS),
    "description": "Object kind. v1 supports deployment / pod / service / namespace / node.",
}
_ANNOTATIONS_PROP: dict[str, Any] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": {"type": ["string", "null"]},
    "description": (
        "Key→value map to merge. A null value removes the key (kubectl ``key-`` syntax)."
    ),
}


_ANNOTATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": _KIND_PROP,
        "name": _NAME_PROP,
        "namespace": {**_NAMESPACE_PROP, "description": "Required for namespaced kinds."},
        "annotations": _ANNOTATIONS_PROP,
    },
    "required": ["kind", "name", "annotations"],
    "additionalProperties": False,
}


_LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": _KIND_PROP,
        "name": _NAME_PROP,
        "namespace": {**_NAMESPACE_PROP, "description": "Required for namespaced kinds."},
        "labels": {
            **_ANNOTATIONS_PROP,
            "description": (
                "Key→value map to merge. A null value removes the key. NOTE: relabeling a "
                "Service-selected workload can re-route traffic."
            ),
        },
    },
    "required": ["kind", "name", "labels"],
    "additionalProperties": False,
}


_CORDON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {**_NAME_PROP, "description": "Node name."},
        "uncordon": {
            "type": "boolean",
            "default": False,
            "description": "Pass true to mark the node schedulable again (reverse of cordon).",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


_SCALE_LLM: dict[str, Any] = {
    "when_to_use": (
        "Set a Deployment's replica count (scale up/down, or scale to 0 to "
        "stop a workload without deleting it). Requires approval."
    ),
    "parameter_hints": {
        "name": "Exact Deployment name.",
        "namespace": "Required.",
        "replicas": "Target count; 0 stops the Deployment.",
    },
    "output_shape": "{name, namespace, replicas_before, replicas_after}.",
}
_ROLLOUT_RESTART_LLM: dict[str, Any] = {
    "when_to_use": (
        "Trigger a rolling restart of every pod in a Deployment (e.g. to "
        "pick up a rotated Secret/ConfigMap). Equivalent to "
        "'kubectl rollout restart'. Requires approval."
    ),
    "parameter_hints": {"name": "Exact Deployment name.", "namespace": "Required."},
    "output_shape": "{name, namespace, restarted_at}.",
}
_NAMESPACE_CREATE_LLM: dict[str, Any] = {
    "when_to_use": (
        "Create a namespace before deploying into it. Idempotent: a "
        "pre-existing namespace returns created=false rather than erroring. "
        "Requires approval."
    ),
    "parameter_hints": {"name": "DNS-1123-label namespace name."},
    "output_shape": "{name, created, already_existed}.",
}
_ANNOTATE_LLM: dict[str, Any] = {
    "when_to_use": (
        "Add/update/remove annotations on a Deployment/Pod/Service/"
        "Namespace/Node. A null value removes the key. Requires approval."
    ),
    "parameter_hints": {
        "kind": "One of deployment/pod/service/namespace/node.",
        "name": "Exact object name.",
        "namespace": "Required for namespaced kinds (everything except namespace/node).",
        "annotations": "Map; null value deletes the key.",
    },
    "output_shape": "{kind, name, namespace, annotations}.",
}
_LABEL_LLM: dict[str, Any] = {
    "when_to_use": (
        "Add/update/remove labels on a Deployment/Pod/Service/Namespace/"
        "Node. CAUTION: relabeling a Service-selected workload can re-route "
        "traffic. A null value removes the key. Requires approval."
    ),
    "parameter_hints": {
        "kind": "One of deployment/pod/service/namespace/node.",
        "name": "Exact object name.",
        "namespace": "Required for namespaced kinds.",
        "labels": "Map; null value deletes the key.",
    },
    "output_shape": "{kind, name, namespace, labels}.",
}
_CORDON_LLM: dict[str, Any] = {
    "when_to_use": (
        "Mark a Node unschedulable (cordon) so new pods avoid it, e.g. "
        "before maintenance. Pass uncordon=true to reverse. Eviction-free "
        "(running pods stay); draining is a separate deferred op. Requires "
        "approval."
    ),
    "parameter_hints": {
        "name": "Node name.",
        "uncordon": "true to make the node schedulable again.",
    },
    "output_shape": "{name, unschedulable, cordoned}.",
}


WRITE_CAUTION_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.scale",
        handler_attr="k8s_scale",
        summary="Set a Deployment's replica count via the /scale subresource.",
        description=(
            "Reads the Deployment's current /scale subresource for the "
            "before-count, then patches it to the requested ``replicas`` "
            "via ``AppsV1Api.patch_namespaced_deployment_scale``. Returns "
            "{name, namespace, replicas_before, replicas_after}. Requires "
            "approval; a human caller's dispatch is parked for review."
        ),
        parameter_schema=_SCALE_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "deployment", "scale"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_SCALE_LLM,
    ),
    KubernetesOp(
        op_id="k8s.rollout.restart",
        handler_attr="k8s_rollout_restart",
        summary="Roll every pod in a Deployment by stamping the restartedAt annotation.",
        description=(
            "Stamps a fresh RFC3339 timestamp into the Deployment's "
            "pod-template ``kubectl.kubernetes.io/restartedAt`` annotation "
            "via ``AppsV1Api.patch_namespaced_deployment``; the controller "
            "rolls every pod under the active strategy. Mirrors 'kubectl "
            "rollout restart'. Requires approval."
        ),
        parameter_schema=_ROLLOUT_RESTART_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "deployment", "rollout"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_ROLLOUT_RESTART_LLM,
    ),
    KubernetesOp(
        op_id="k8s.namespace.create",
        handler_attr="k8s_namespace_create",
        summary="Create a namespace; idempotent (a 409 is reported, not raised).",
        description=(
            "Creates a namespace via ``CoreV1Api.create_namespace``. A 409 "
            "Conflict (already exists) is swallowed and reported as "
            "created=false so the op is safe to retry and to run as a "
            "deploy precondition. Requires approval."
        ),
        parameter_schema=_NAMESPACE_CREATE_SCHEMA,
        response_schema=None,
        group_key="inventory",
        tags=("write", "namespace"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_NAMESPACE_CREATE_LLM,
    ),
    KubernetesOp(
        op_id="k8s.annotate",
        handler_attr="k8s_annotate",
        summary="Add/update/remove annotations on a Deployment/Pod/Service/Namespace/Node.",
        description=(
            "Strategic-merge patches ``metadata.annotations`` over a "
            "kind→method dispatch table (v1: deployment / pod / service / "
            "namespace / node). A null value removes the key (kubectl "
            "``key-`` syntax). Requires approval."
        ),
        parameter_schema=_ANNOTATE_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "metadata", "annotate"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_ANNOTATE_LLM,
    ),
    KubernetesOp(
        op_id="k8s.label",
        handler_attr="k8s_label",
        summary="Add/update/remove labels on a Deployment/Pod/Service/Namespace/Node.",
        description=(
            "Strategic-merge patches ``metadata.labels`` over the same "
            "kind→method dispatch table as ``k8s.annotate``. A null value "
            "removes the key. NOTE: relabeling a Service-selected workload "
            "can re-route traffic, hence caution. Requires approval."
        ),
        parameter_schema=_LABEL_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "metadata", "label"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_LABEL_LLM,
    ),
    KubernetesOp(
        op_id="k8s.cordon",
        handler_attr="k8s_cordon",
        summary="Mark a Node (un)schedulable. Reversible, eviction-free.",
        description=(
            "Toggles a Node's ``spec.unschedulable`` via "
            "``CoreV1Api.patch_node``. ``uncordon=true`` reverses it. "
            "Eviction-free -- running pods are untouched (draining is the "
            "deferred ``k8s.drain``). Requires approval."
        ),
        parameter_schema=_CORDON_SCHEMA,
        response_schema=None,
        group_key="inventory",
        tags=("write", "node", "cordon"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_CORDON_LLM,
    ),
)


# ---------------------------------------------------------------------------
# Dangerous-class schemas + LLM instructions
# ---------------------------------------------------------------------------


_APPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "manifest": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Single- or multi-document YAML/JSON manifest. Each document "
                "must carry apiVersion + kind + metadata.name."
            ),
        },
        "dry_run": {
            "type": "string",
            "enum": ["none", "server"],
            "default": "none",
            "description": (
                "'server' runs SSA with ?dryRun=All (mutates nothing; returns "
                "the would-be object as a preview). 'none' persists."
            ),
        },
    },
    "required": ["manifest"],
    "additionalProperties": False,
}


_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": sorted(DELETABLE_KINDS),
            "description": "v1 supports pod / job / replicaset only.",
        },
        "name": _NAME_PROP,
        "namespace": _NAMESPACE_PROP,
        "propagation_policy": {
            "type": "string",
            "enum": ["Foreground", "Background", "Orphan"],
            "description": (
                "Cascade behaviour. Foreground: delete dependents first. "
                "Background: delete object then reap dependents. Orphan: leave "
                "dependents. No default -- the API server's per-kind default "
                "applies when omitted."
            ),
        },
        "grace_period_seconds": {
            "type": "integer",
            "minimum": 0,
            "description": "Override the object's termination grace period. 0 = immediate.",
        },
    },
    "required": ["kind", "name", "namespace"],
    "additionalProperties": False,
}


_SECRET_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROP,
        "namespace": _NAMESPACE_PROP,
        "type": {
            "type": "string",
            "minLength": 1,
            "default": "Opaque",
            "description": "Secret type (Opaque, kubernetes.io/dockerconfigjson, etc.).",
        },
        "string_data": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Plaintext key→value map; the API server base64-encodes it.",
        },
        "data": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Already-base64-encoded key→value map.",
        },
    },
    "required": ["name", "namespace"],
    "anyOf": [{"required": ["string_data"]}, {"required": ["data"]}],
    "additionalProperties": False,
}


_JOB_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROP,
        "namespace": _NAMESPACE_PROP,
        "spec": {
            "type": "object",
            "minProperties": 1,
            "description": (
                "The Job's ``spec`` body (template, backoffLimit, etc.). "
                "Inline env secrets are redacted from the broadcast."
            ),
        },
    },
    "required": ["name", "namespace", "spec"],
    "additionalProperties": False,
}


_APPLY_LLM: dict[str, Any] = {
    "when_to_use": (
        "Apply a manifest (replaces 'kubectl apply -f'). Run with "
        "dry_run='server' first to preview the would-be result with no "
        "side effects, then dry_run='none' to persist. Requires approval."
    ),
    "parameter_hints": {
        "manifest": "YAML/JSON; multi-doc supported via '---'.",
        "dry_run": "'server' for a side-effect-free preview; 'none' to apply.",
    },
    "output_shape": (
        "{dry_run, field_manager, applied: [{api_version, kind, name, namespace, "
        "resource_version, uid}], total}."
    ),
}
_DELETE_LLM: dict[str, Any] = {
    "when_to_use": (
        "Delete a pod, job, or replicaset by name. v1 does NOT support "
        "deleting namespaces/PVCs/PVs. Set propagation_policy + "
        "grace_period_seconds explicitly to control cascade + termination. "
        "Requires approval."
    ),
    "parameter_hints": {
        "kind": "pod | job | replicaset.",
        "name": "Exact object name.",
        "namespace": "Required.",
        "propagation_policy": "Foreground | Background | Orphan.",
        "grace_period_seconds": "0 for immediate deletion.",
    },
    "output_shape": "{kind, name, namespace, propagation_policy, grace_period_seconds, deleted}.",
}
_SECRET_CREATE_LLM: dict[str, Any] = {
    "when_to_use": (
        "Create a Secret. Pass plaintext under string_data (API encodes) or "
        "pre-encoded under data. Values are NEVER echoed back or broadcast "
        "(credential_write redaction). Requires approval."
    ),
    "parameter_hints": {
        "name": "Secret name.",
        "namespace": "Required.",
        "type": "Opaque by default.",
        "string_data": "Plaintext key→value.",
        "data": "Base64-encoded key→value.",
    },
    "output_shape": "{name, namespace, type, data_keys, created} -- key names only, no values.",
}
_JOB_CREATE_LLM: dict[str, Any] = {
    "when_to_use": (
        "Create a batch Job from a spec body. The pod template may carry "
        "inline env secrets, so the broadcast is redacted "
        "(credential_write). Requires approval."
    ),
    "parameter_hints": {
        "name": "Job name.",
        "namespace": "Required.",
        "spec": "The Job spec (template, backoffLimit, completions, ...).",
    },
    "output_shape": "{name, namespace, created} -- spec body is never echoed.",
}


WRITE_DANGEROUS_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.apply",
        handler_attr="k8s_apply",
        summary="Server-side apply a manifest; dry_run='server' previews with no side effects.",
        description=(
            "Parses a single- or multi-document manifest, resolves each "
            "document's GVK via the dynamic client's discovery, and "
            "server-side-applies it under the 'meho' field manager. "
            "dry_run='server' runs every doc with the API's ?dryRun=All so "
            "nothing is persisted -- the preview the approval reviewer reads. "
            "Replaces 'kubectl apply -f'. Requires approval."
        ),
        parameter_schema=_APPLY_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "apply", "dangerous"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_APPLY_LLM,
    ),
    KubernetesOp(
        op_id="k8s.delete",
        handler_attr="k8s_delete",
        summary="Delete a pod/job/replicaset by name (v1 scope); explicit cascade params.",
        description=(
            "Deletes via a kind→method dispatch table scoped to "
            "pod / job / replicaset in v1 (namespace / PVC / PV excluded -- "
            "their cascading data loss is out of scope). "
            "propagation_policy + grace_period_seconds are explicit so the "
            "cascade is never an implicit default. Requires approval."
        ),
        parameter_schema=_DELETE_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "delete", "dangerous"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_DELETE_LLM,
    ),
    KubernetesOp(
        op_id="k8s.secret.create",
        handler_attr="k8s_secret_create",
        summary="Create a Secret; values are redacted from audit + broadcast.",
        description=(
            "Creates a Secret from string_data (plaintext) and/or data "
            "(base64). The values are written to the cluster but never "
            "echoed back -- the response is key-names-only -- and the op "
            "classifies credential_write so the broadcast collapses to "
            "aggregate-only. Requires approval."
        ),
        parameter_schema=_SECRET_CREATE_SCHEMA,
        response_schema=None,
        group_key="config",
        tags=("write", "secret", "credential", "dangerous"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_SECRET_CREATE_LLM,
    ),
    KubernetesOp(
        op_id="k8s.job.create",
        handler_attr="k8s_job_create",
        summary="Create a batch Job from a spec body; inline env secrets redacted.",
        description=(
            "Creates a batch/v1 Job from a spec body. The pod template can "
            "carry inline env secret material, so the op classifies "
            "credential_write and the broadcast collapses to aggregate-only. "
            "The response echoes the Job's identity only. Requires approval."
        ),
        parameter_schema=_JOB_CREATE_SCHEMA,
        response_schema=None,
        group_key="workload",
        tags=("write", "job", "credential", "dangerous"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_JOB_CREATE_LLM,
    ),
)
