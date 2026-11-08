# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and guidance texts of ``k8s.node.disk`` and ``k8s.node.images``.

Kept apart from ``ops_node_disk.py`` (the handlers) to stay under the file
size cap. This module has no ``OPS``, so the collector skips it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "K8S_NODE_DISK_LLM_INSTRUCTIONS",
    "K8S_NODE_DISK_PARAMETER_SCHEMA",
    "K8S_NODE_DISK_RESPONSE_SCHEMA",
    "K8S_NODE_IMAGES_LLM_INSTRUCTIONS",
    "K8S_NODE_IMAGES_PARAMETER_SCHEMA",
    "K8S_NODE_IMAGES_RESPONSE_SCHEMA",
]

_PRESENT = "present"
_NOT_PRESENT = "not present"
_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Parameter schemas, response schemas, LLM instructions, registration
# ---------------------------------------------------------------------------

_NODE_PARAMETER: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Node name. Leave out to read every node (at most 50).",
}

K8S_NODE_DISK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"node": _NODE_PARAMETER},
    "additionalProperties": False,
    "examples": [{}, {"node": "worker-1"}],
}

K8S_NODE_IMAGES_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"node": _NODE_PARAMETER},
    "additionalProperties": False,
    "examples": [{}, {"node": "worker-1"}],
}

_FS: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "capacity_bytes": {"type": ["integer", "null"]},
        "used_bytes": {"type": ["integer", "null"]},
        "available_bytes": {"type": ["integer", "null"]},
        "used_percent": {"type": ["number", "null"]},
    },
    "additionalProperties": False,
}

_STATE_PART: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": [_PRESENT, _UNKNOWN]},
        "error": {"type": "string"},
    },
    "required": ["state"],
    "additionalProperties": False,
}

_NODE_STATE: dict[str, Any] = {
    "type": "string",
    "enum": [_NOT_PRESENT],
    "description": "Set only when the named node does not exist.",
}

K8S_NODE_DISK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "status": {"type": "string"},
                                "reason": {"type": ["string", "null"]},
                                "last_transition_time": {"type": ["string", "null"]},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "summary": _STATE_PART,
                    "node_fs": _FS,
                    "image_fs": _FS,
                    "container_fs": _FS,
                },
                "required": ["name", "conditions", "summary"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
        "incomplete": {"type": "boolean"},
        "node_state": _NODE_STATE,
    },
    "required": ["rows", "total", "incomplete"],
    "additionalProperties": False,
}

K8S_NODE_IMAGES_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "image_count": {"type": "integer"},
                    "total_size_bytes": {"type": "integer"},
                    "list_may_be_cut": {"type": "boolean"},
                    "pods": _STATE_PART,
                    "unused_count": {"type": ["integer", "null"]},
                    "unused_size_bytes": {"type": ["integer", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node": {"type": ["string", "null"]},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "size_bytes": {"type": ["integer", "null"]},
                    "used_by_running_pod": {"type": ["boolean", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
        "incomplete": {"type": "boolean"},
        "node_state": _NODE_STATE,
    },
    "required": ["nodes", "rows", "total", "incomplete"],
    "additionalProperties": False,
}

K8S_NODE_DISK_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to see how full a node's disk is and whether the node reports "
        "disk, memory or process pressure. Use it after an eviction or a "
        "'node not ready' alert to see whether the node has recovered. It "
        "changes nothing."
    ),
    "preconditions": (
        "The target's login must be allowed to read nodes and the node "
        "proxy (nodes/proxy). Without that right the sizes are 'unknown', "
        "but the conditions still show."
    ),
    "parameter_hints": {"node": "Optional node name. Leave out to read every node."},
    "output_shape": (
        "{rows: [{name, conditions: [{type, status, reason, last_transition_time}], "
        "summary: {state, error?}, node_fs, image_fs, container_fs}], total, incomplete}. "
        "Each file system has capacity_bytes, used_bytes, available_bytes, "
        "used_percent. A file system is null when the kubelet does not report it "
        "(container_fs only exists on newer kubelets). summary.state 'unknown' "
        "means the sizes could not be read: it is not an empty disk."
    ),
    "next_step": (
        "Call k8s.node.images to see which images fill the image store, and "
        "which no running pod uses."
    ),
}

K8S_NODE_IMAGES_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to see which container images a node holds, how big they are, "
        "and which no running pod uses. Use it to answer 'is the big image "
        "still on node X?'. It changes nothing."
    ),
    "preconditions": ("The target's login must be allowed to list nodes and pods."),
    "parameter_hints": {"node": "Optional node name. Leave out to read every node."},
    "output_shape": (
        "{nodes: [{name, image_count, total_size_bytes, list_may_be_cut, pods, "
        "unused_count, unused_size_bytes}], rows: [{node, names, size_bytes, "
        "used_by_running_pod}], total, incomplete}. Rows are sorted by size, "
        "biggest first, per node. names holds the tags and digests of one image. "
        "list_may_be_cut true means the node lists 50 images, the kubelet default "
        "limit, so more may exist. used_by_running_pod is null (never false) when "
        "the pods could not be read: see nodes[].pods."
    ),
    "next_step": (
        "Call k8s.node.disk to see free space and pressure conditions. Deleting "
        "an image is a change on the node and is not part of this op."
    ),
}
