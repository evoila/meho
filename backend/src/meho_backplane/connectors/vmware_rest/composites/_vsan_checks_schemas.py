# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of the vSAN proactive tests and object repair (#4446)."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._vsan_schemas import (
    _CLUSTER,
    _GUIDANCE,
    _INT_OR_NULL,
    _ISSUES,
    _STATUS_READ,
    _STR_OR_NULL,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import DATASTORE_MOID_PATTERN

__all__ = [
    "OBJECT_REPAIR_LLM_INSTRUCTIONS",
    "OBJECT_REPAIR_PARAMETER_SCHEMA",
    "OBJECT_REPAIR_RESPONSE_SCHEMA",
    "PROACTIVE_TEST_LLM_INSTRUCTIONS",
    "PROACTIVE_TEST_PARAMETER_SCHEMA",
    "PROACTIVE_TEST_RESPONSE_SCHEMA",
]

# ---------------------------------------------------------------------------
# vmware.composite.vsan.proactive_test.run
# ---------------------------------------------------------------------------

PROACTIVE_TEST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": _CLUSTER,
        "test": {
            "type": "string",
            "enum": ["vm_creation", "network_performance", "storage_load"],
            "description": "vm_creation: a small VM on each host. network_performance: network "
            "load between the hosts. storage_load: storage load on each host.",
        },
        "duration_seconds": {
            "type": "integer",
            "minimum": 5,
            "maximum": 3600,
            "description": "Test time. vm_creation and network_performance: 5 to 25 "
            "(default 20). storage_load: 60 to 3600 (default 300).",
        },
        "datastore": {
            "type": "string",
            "pattern": DATASTORE_MOID_PATTERN,
            "description": "vm_creation only: the datastore for the test VM. Default: the "
            "vSAN datastore of the cluster.",
        },
        "workload": {
            "type": "string",
            "minLength": 1,
            "description": "storage_load only, required: the load profile id. A wrong id "
            "gives the list of known ids.",
        },
        "run_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "storage_load only: a name for the run.",
        },
    },
    "required": ["cluster", "test"],
    "additionalProperties": False,
    "oneOf": [
        {"properties": {"test": {"enum": ["vm_creation"], "description": "The VM test."}}},
        {
            "properties": {
                "test": {"enum": ["network_performance"], "description": "The network test."}
            }
        },
        {
            "properties": {"test": {"enum": ["storage_load"], "description": "The storage test."}},
            "required": ["workload"],
        },
    ],
    "examples": [{"cluster": "domain-c7", "test": "vm_creation"}],
}

_HOST_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": {**_STR_OR_NULL, "description": "The host name."},
        "status": {**_STR_OR_NULL, "description": "The vendor's result for the host."},
        "error": {**_STR_OR_NULL, "description": "The vendor's error text, if any."},
        "bandwidth_bps": {**_INT_OR_NULL, "description": "network_performance: bandwidth."},
        "loss_percent": {**_INT_OR_NULL, "description": "network_performance: packet loss."},
        "jitter_ms": {"type": ["number", "null"], "description": "network_performance: jitter."},
        "iops": {**_INT_OR_NULL, "description": "storage_load: I/O per second of one disk."},
        "throughput_bps": {**_INT_OR_NULL, "description": "storage_load: bytes per second."},
        "avg_latency_us": {**_INT_OR_NULL, "description": "storage_load: average latency."},
        "max_latency_us": {**_INT_OR_NULL, "description": "storage_load: highest latency."},
    },
}

PROACTIVE_TEST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "failed", "not_confirmed", "invalid_request"],
            "description": "completed: the test ran; read overall_status.",
        },
        "cluster": {"type": "string", "description": "The cluster."},
        "test": {"type": "string", "description": "The test that ran."},
        "overall_status": {**_STR_OR_NULL, "description": "The vendor's overall result."},
        "overall_status_description": {**_STR_OR_NULL, "description": "Its text."},
        "checked_at": {**_STR_OR_NULL, "description": "When the test ran."},
        "hosts": {
            "type": "array",
            "items": _HOST_ROW,
            "description": "One row per host (storage_load: per test disk).",
        },
        "task": {"type": "string", "description": "storage_load: the vCenter task id."},
        "status_read": _STATUS_READ,
        "error": {**_STR_OR_NULL, "description": "The vendor's error text of a failed task."},
        "workloads": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Known storage load profile ids, after a wrong workload.",
        },
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "guidance"],
}

PROACTIVE_TEST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to check a vSAN cluster after a change or when it is slow: can each host create "
        "a VM, how fast is the network between the hosts, how fast is the storage. The "
        "network and storage tests put real load on the cluster; run them in a quiet time."
    ),
    "preconditions": "The operator agrees to the load. storage_load runs long: use async: true.",
    "parameter_hints": {
        "test": "vm_creation, network_performance or storage_load.",
        "duration_seconds": "5 to 25 for the short tests; 60 to 3600 for storage_load.",
        "workload": "storage_load only; call once with any id to get the known ids.",
    },
    "output_shape": (
        "{status, cluster, test, overall_status, overall_status_description, checked_at, "
        "hosts: [{host, status, error, bandwidth_bps, loss_percent, jitter_ms, iops, "
        "throughput_bps, avg_latency_us, max_latency_us}], guidance}. A long host list comes "
        "back behind a result handle; read it with result_query."
    ),
    "next_step": "Report overall_status and the hosts that did not pass.",
}

# ---------------------------------------------------------------------------
# vmware.composite.vsan.object.repair
# ---------------------------------------------------------------------------

OBJECT_REPAIR_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": _CLUSTER,
        "object_uuids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1000,
            "items": {
                "type": "string",
                "minLength": 1,
                "description": "A vSAN object UUID, for example <object-uuid>.",
            },
            "description": "Only these objects. Leave out for every object that needs a repair.",
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}],
}

OBJECT_REPAIR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["repair_queued", "failed", "not_confirmed", "invalid_request"],
            "description": "repair_queued: the objects are in the repair queue.",
        },
        "cluster": {"type": "string", "description": "The cluster."},
        "task": {**_STR_OR_NULL, "description": "The vCenter task id."},
        "status_read": _STATUS_READ,
        "error": {**_STR_OR_NULL, "description": "The vendor's error text of a failed task."},
        "object_uuids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The objects asked for; empty means all.",
        },
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "guidance"],
}

OBJECT_REPAIR_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call when vSAN objects have reduced redundancy (for example after a host failed) and "
        "the operator wants the repair now, without waiting for the vSAN repair timer."
    ),
    "preconditions": (
        "vmware.host.vsan_health shows the object health problem. The repair adds disk and "
        "network load."
    ),
    "parameter_hints": {
        "cluster": "The cluster moid, for example <cluster-id>.",
        "object_uuids": "Optional list of object UUIDs; leave out for all.",
    },
    "output_shape": (
        "{status: repair_queued|failed|not_confirmed|invalid_request, cluster, task, "
        "status_read, error, object_uuids, guidance}."
    ),
    "next_step": "Check the object health again with vmware.host.vsan_health later.",
}
