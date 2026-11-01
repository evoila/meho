# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of the five host membership changes (#4349).

``vmware.composite.host.{add,move,connect,disconnect,remove}``; the handlers
are in :mod:`._host_membership` and :mod:`._host_remove`. The status read of
``host.add`` is the typed ``vmware.task.info``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._vcenter_deploy_schemas import (
    secret_ref_fields,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_MOID_PATTERN,
    HOST_MOID_PATTERN,
)

__all__ = [
    "HOST_ADD_LLM_INSTRUCTIONS",
    "HOST_ADD_PARAMETER_SCHEMA",
    "HOST_ADD_RESPONSE_SCHEMA",
    "HOST_CONNECT_LLM_INSTRUCTIONS",
    "HOST_CONNECT_RESPONSE_SCHEMA",
    "HOST_DISCONNECT_LLM_INSTRUCTIONS",
    "HOST_DISCONNECT_RESPONSE_SCHEMA",
    "HOST_FOLDER_MOID_PATTERN",
    "HOST_MOVE_LLM_INSTRUCTIONS",
    "HOST_MOVE_PARAMETER_SCHEMA",
    "HOST_MOVE_RESPONSE_SCHEMA",
    "HOST_ONLY_PARAMETER_SCHEMA",
    "HOST_REMOVE_LLM_INSTRUCTIONS",
    "HOST_REMOVE_RESPONSE_SCHEMA",
    "SHA1_THUMBPRINT_PATTERN",
]

#: A host folder of a datacenter (``group-h4``): the folder that holds hosts
#: and clusters, not a VM folder (``group-v``).
HOST_FOLDER_MOID_PATTERN: Final = r"^group-h[0-9]+$(?!\n)"
#: The SHA-1 thumbprint of the host certificate, as vCenter wants it.
SHA1_THUMBPRINT_PATTERN: Final = r"^([0-9A-Fa-f]{2}:){19}[0-9A-Fa-f]{2}$(?!\n)"

_EXAMPLE_THUMBPRINT: Final = "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33"

_HOST_FIELD: dict[str, Any] = {
    "type": "string",
    "pattern": HOST_MOID_PATTERN,
    "description": "Host id (moid), e.g. host-1001. list_targets or GET:/api/vcenter/host.",
}

_NULLABLE_TEXT: Final = ["string", "null"]

_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Present when the change is not confirmed (for example a timeout).",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Where the problem is."},
            "severity": {"type": "string", "description": "error, warning or info."},
            "message": {"type": "string", "description": "What happened."},
        },
    },
}

#: The host fields each answer carries: the state MEHO read last.
_HOST_STATE_PROPERTIES: dict[str, Any] = {
    "host": {"type": "string", "description": "The host id (moid) the call named."},
    "name": {"type": _NULLABLE_TEXT, "description": "The host's name in vCenter."},
    "connection_state": {
        "type": _NULLABLE_TEXT,
        "description": "connected, disconnected or notResponding, as read last.",
    },
    "in_maintenance_mode": {
        "type": ["boolean", "null"],
        "description": "Whether the host is in maintenance mode, as read last.",
    },
    "parent": {
        "type": _NULLABLE_TEXT,
        "description": "Id of the cluster (domain-c...) or the single-host resource (domain-s...).",
    },
    "parent_type": {
        "type": _NULLABLE_TEXT,
        "description": "ClusterComputeResource (in a cluster) or ComputeResource (alone).",
    },
    "vm_count": {
        "type": ["integer", "null"],
        "description": "How many VMs are registered on the host.",
    },
    "task": {
        "type": _NULLABLE_TEXT,
        "description": "The vCenter task id of the change; null when nothing was sent.",
    },
    "guidance": {"type": _NULLABLE_TEXT, "description": "What to do next, in plain words."},
    "issues": _ISSUES_SCHEMA,
}


def _state_response(statuses: list[str], status_text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses, "description": status_text},
            **_HOST_STATE_PROPERTIES,
            **extra,
        },
        "required": ["status", "host"],
    }


# ===========================================================================
# host.add
# ===========================================================================

HOST_ADD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "pattern": CLUSTER_MOID_PATTERN,
            "description": "Add the host to this cluster (moid, e.g. domain-c7). Or give folder.",
        },
        "folder": {
            "type": "string",
            "pattern": HOST_FOLDER_MOID_PATTERN,
            "description": (
                "Add the host alone (not in a cluster) to this host folder of a datacenter "
                "(moid, e.g. group-h4). Or give cluster."
            ),
        },
        "host_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "DNS name or IP address of the ESXi host to add.",
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "HTTPS port of the host (default 443).",
        },
        "user_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "Administrator account on the host (default root).",
        },
        **secret_ref_fields("password", "the password of user_name on the host"),
        "ssl_thumbprint": {
            "type": "string",
            "pattern": SHA1_THUMBPRINT_PATTERN,
            "description": (
                "SHA-1 thumbprint of the host certificate (AA:BB:... 20 pairs). vCenter adds "
                "the host only when the host shows this certificate."
            ),
        },
    },
    "required": ["host_name", "password_secret_ref", "ssl_thumbprint"],
    "oneOf": [{"required": ["cluster"]}, {"required": ["folder"]}],
    "additionalProperties": False,
    "examples": [
        {
            "cluster": "domain-c7",
            "host_name": "<esxi-host-name>",
            "password_secret_ref": "<vault-path-of-the-host-password>",
            "ssl_thumbprint": _EXAMPLE_THUMBPRINT,
        }
    ],
}

_ADD_STATUSES: Final = [
    "accepted",
    "already_added",
    "already_running",
    "parent_not_found",
    "needs_vcenter",
    "start_failed",
    "start_unknown",
]

HOST_ADD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": _ADD_STATUSES,
            "description": (
                "accepted: vCenter took the job; it is not done yet. already_added: a host "
                "with this name is in vCenter; nothing sent. already_running: an add runs on "
                "this cluster or folder; nothing sent. parent_not_found, needs_vcenter: "
                "nothing sent. start_failed: vCenter refused the start. start_unknown: no "
                "reply to the start, or a gateway answered HTTP 502, 503 or 504 for it; the "
                "job may run."
            ),
        },
        "host_name": {"type": "string", "description": "The host name the call named."},
        "parent": {"type": "string", "description": "The cluster or host folder id."},
        "parent_type": {
            "type": "string",
            "enum": ["cluster", "folder"],
            "description": "cluster: added to a cluster. folder: added alone.",
        },
        "parent_name": {"type": _NULLABLE_TEXT, "description": "Name of the cluster or folder."},
        "task": {
            "type": _NULLABLE_TEXT,
            "description": "The vCenter task id of the add (also of a running add).",
        },
        "host": {
            "type": _NULLABLE_TEXT,
            "description": "On already_added: the id of the host already in vCenter.",
        },
        "status_read": {
            "type": ["object", "null"],
            "description": "The read that follows the job: call it with these params.",
            "properties": {
                "op_id": {"type": "string", "description": "Always vmware.task.info."},
                "params": {"type": "object", "description": "{task: <task id>}."},
            },
        },
        "error_type": {
            "type": _NULLABLE_TEXT,
            "description": "On start_failed: the vendor's fault type, e.g. InvalidLogin.",
        },
        "error_message": {
            "type": _NULLABLE_TEXT,
            "description": (
                "On start_failed: the vendor's error text. On start_unknown: the error class "
                "or the HTTP status of the answer that was not from vCenter."
            ),
        },
        "guidance": {"type": "string", "description": "What to do next, in plain words."},
    },
    "required": ["status", "host_name", "parent", "parent_type", "guidance"],
}

HOST_ADD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to add a new ESXi host to a cluster (cluster) or alone to a host folder "
        "(folder). A second person must approve it. Use MEHO's async mode (async: true): "
        "MEHO then keeps the answer with the task id under a run handle, so a lost reply "
        "does not lose the task id."
    ),
    "preconditions": (
        "The host's root password is in Vault (password_secret_ref). You know the SHA-1 "
        "thumbprint of the host certificate (from the host console, or openssl x509 "
        "-fingerprint -sha1 on its certificate). The host is not managed by another vCenter. "
        "Works only on a vCenter target."
    ),
    "parameter_hints": {
        "cluster": "Cluster id from vmware.cluster.list. Give cluster or folder, not both.",
        "folder": "Host folder id (group-h...) of the datacenter, for a host outside a cluster.",
        "password_secret_ref": "Vault path of the secret with the password (key 'password').",
    },
    "output_shape": (
        "{status: accepted|already_added|already_running|parent_not_found|needs_vcenter|"
        "start_failed|start_unknown, host_name, parent, parent_type, parent_name, task, host, "
        "status_read: {op_id, params}, error_type, error_message, guidance}. accepted means "
        "vCenter started the job, not that the host is added."
    ),
    "next_step": (
        "accepted or already_running: call vmware.task.info with the task id until the state "
        "is success (result names the new host or its compute resource) or error. After "
        "start_unknown (a lost reply or a gateway error), do not start again blindly: call "
        "host.add again; it answers "
        "already_added or already_running when the first call worked. Then license the host "
        "(license.assign) and add it to a distributed switch."
    ),
}

# ===========================================================================
# host.move / host.connect / host.disconnect
# ===========================================================================

HOST_MOVE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_FIELD,
        "cluster": {
            "type": "string",
            "pattern": CLUSTER_MOID_PATTERN,
            "description": "The cluster to move the host into (moid, e.g. domain-c7).",
        },
    },
    "required": ["host", "cluster"],
    "additionalProperties": False,
    "examples": [{"host": "host-1001", "cluster": "domain-c7"}],
}

HOST_ONLY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"host": _HOST_FIELD},
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "host-1001"}],
}

_COMMON_STATUS_TEXT: Final = (
    " not_found: no host with this id; nothing sent. needs_vcenter: the target is a "
    "single ESXi host; nothing sent. not_verified: the task ended but the host does not "
    "read back as asked. timeout: MEHO stopped waiting; the task may still run (task)."
)

HOST_MOVE_RESPONSE_SCHEMA: dict[str, Any] = _state_response(
    [
        "moved",
        "unchanged",
        "not_found",
        "cluster_not_found",
        "not_in_maintenance_mode",
        "needs_vcenter",
        "not_verified",
        "timeout",
    ],
    "moved: the host is in the cluster now. unchanged: it already was; nothing sent. "
    "cluster_not_found, not_in_maintenance_mode: nothing sent." + _COMMON_STATUS_TEXT,
    cluster={"type": "string", "description": "The cluster id the call named."},
)

HOST_CONNECT_RESPONSE_SCHEMA: dict[str, Any] = _state_response(
    ["connected", "unchanged", "not_found", "needs_vcenter", "not_verified", "timeout"],
    "connected: vCenter reconnected the host. unchanged: it already was connected; nothing "
    "sent." + _COMMON_STATUS_TEXT,
)

HOST_DISCONNECT_RESPONSE_SCHEMA: dict[str, Any] = _state_response(
    ["disconnected", "unchanged", "not_found", "needs_vcenter", "not_verified", "timeout"],
    "disconnected: vCenter no longer manages the host; its VMs keep running. unchanged: it "
    "already was disconnected; nothing sent." + _COMMON_STATUS_TEXT,
)

_STATE_OUTPUT: Final = (
    "host, name, connection_state, in_maintenance_mode, parent, parent_type, vm_count, task, "
    "guidance, issues}. The host fields are read back after the task ended."
)

HOST_MOVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to move a host that vCenter already manages into a cluster of the same "
        "datacenter, for example a host added alone. A second person must approve it."
    ),
    "preconditions": (
        "The host is in maintenance mode (else not_in_maintenance_mode, nothing sent); use "
        "host.evacuate first. Every VM registered on the host moves with it."
    ),
    "parameter_hints": {"cluster": "Cluster id from vmware.cluster.list."},
    "output_shape": (
        "{status: moved|unchanged|not_found|cluster_not_found|not_in_maintenance_mode|"
        "needs_vcenter|not_verified|timeout, cluster, " + _STATE_OUTPUT
    ),
    "next_step": (
        "On moved, take the host out of maintenance mode (host.maintenance.exit) when it is "
        "ready. On timeout, read the task with vmware.task.info. A vendor fault (for example "
        "InvalidArgument: another datacenter) fails the call with the fault text."
    ),
}

HOST_CONNECT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to reconnect a host that is disconnected or not responding in vCenter, with "
        "the login vCenter already has. A second person must approve it."
    ),
    "preconditions": (
        "The host runs and vCenter can reach it on its management network. A host that is "
        "connected answers unchanged and nothing is sent, so the call is safe to run twice."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{status: connected|unchanged|not_found|needs_vcenter|not_verified|timeout, "
        + _STATE_OUTPUT
    ),
    "next_step": (
        "On connected, check the host (vmware.host.usage). A vendor fault fails the call "
        "with the fault text (for example InvalidLogin: the stored login no longer works; "
        "remove and add the host again, or fix the login on the host)."
    ),
}

HOST_DISCONNECT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to disconnect a host from vCenter, for example before host work that would "
        "make vCenter raise alarms. Its VMs keep running, but vCenter cannot manage them, "
        "and vSphere HA does not protect them. A second person must approve it."
    ),
    "preconditions": (
        "You accept that vCenter cannot manage the host and its VMs until host.connect. A "
        "host that is disconnected answers unchanged and nothing is sent."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{status: disconnected|unchanged|not_found|needs_vcenter|not_verified|timeout, "
        + _STATE_OUTPUT
    ),
    "next_step": "Reconnect it with host.connect, or remove it with host.remove.",
}

# ===========================================================================
# host.remove
# ===========================================================================

HOST_REMOVE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "removed",
                "unchanged",
                "precondition_failed",
                "needs_vcenter",
                "still_present",
                "timeout",
            ],
            "description": (
                "removed: the host is gone from vCenter. unchanged: no host with this id; "
                "nothing sent. precondition_failed: the host is connected and not in "
                "maintenance mode; nothing sent. needs_vcenter: nothing sent. still_present: "
                "the task ended but the host reads back. timeout: the task may still run."
            ),
        },
        "object": {
            "type": "object",
            "description": "The host as read before the change.",
            "properties": {
                "kind": {"type": "string", "description": "Always host."},
                "moid": {"type": "string", "description": "The host id."},
                "name": {"type": _NULLABLE_TEXT, "description": "The host's name."},
                "parent": {"type": _NULLABLE_TEXT, "description": "Cluster or resource id."},
                "parent_type": {"type": _NULLABLE_TEXT, "description": "Type of the parent."},
                "connection_state": {"type": _NULLABLE_TEXT, "description": "Connection state."},
                "in_maintenance_mode": {
                    "type": ["boolean", "null"],
                    "description": "Maintenance mode.",
                },
                "vm_count": {"type": "integer", "description": "VMs registered on the host."},
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "object", "description": "What stops the remove."},
            "description": "Empty unless the status is precondition_failed.",
        },
        "task": {"type": _NULLABLE_TEXT, "description": "The vCenter task id of the remove."},
        "task_state": {"type": _NULLABLE_TEXT, "description": "The task's last state."},
        "guidance": {"type": _NULLABLE_TEXT, "description": "What to do next."},
    },
    "required": ["status", "object"],
}

HOST_REMOVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to remove a host from vCenter for good, for example a broken host or one you "
        "give back. A person must run it: call preview_operation first, then call_operation "
        "with the preview_hash. A second person approves it after reading the preview."
    ),
    "preconditions": (
        "The host is in maintenance mode, or it is not connected (disconnected or not "
        "responding). Else precondition_failed and nothing is sent; use host.evacuate first."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{status: removed|unchanged|precondition_failed|needs_vcenter|still_present|timeout, "
        "object: {kind, moid, name, parent, parent_type, connection_state, "
        "in_maintenance_mode, vm_count}, blockers, task, task_state, guidance}."
    ),
    "next_step": (
        "On removed, the VMs that were registered on the host are gone from vCenter too; "
        "their files stay on the datastores. To use the host again, add it with host.add. "
        "On timeout, read the task with vmware.task.info."
    ),
}
