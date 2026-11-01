# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the SNMP and core dump changes.

The handlers are in :mod:`._host_access_writes`; the shared answer shape is in
:mod:`._host_security_schemas`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._host_security_schemas import (
    NEXT_STEP_TAIL,
    NULL_STR,
    op_parts,
    response_schema,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_security import HOST_PARAMETER

__all__ = ["COREDUMP_SET", "SNMP_SET"]

#: A host name or an IP address, with no space, comma or line break.
_HOST_NAME_PATTERN: Final = r"^[A-Za-z0-9.:\-\[\]]+$(?!\n)"

# ---------------------------------------------------------------------------
# host.snmp.set
# ---------------------------------------------------------------------------

_SNMP_STATE: Final = {
    "type": ["object", "null"],
    "description": "SNMP agent settings; community strings are counted, never shown.",
    "properties": {
        "enabled": {"type": "boolean", "description": "The agent is on."},
        "port": {"type": ["integer", "null"], "description": "UDP port of the agent."},
        "read_only_community_count": {
            "type": "integer",
            "description": "How many read-only communities are set.",
        },
        "trap_targets": {
            "type": "array",
            "description": "Where traps go.",
            "items": {
                "type": "object",
                "properties": {
                    "host_name": {**NULL_STR, "description": "Trap receiver."},
                    "port": {"type": ["integer", "null"], "description": "Receiver port."},
                },
            },
        },
    },
}

SNMP_SET: Final[dict[str, Any]] = op_parts(
    parameters={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "enabled": {"type": "boolean", "description": "true turns the agent on."},
            "port": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "description": "UDP port of the agent (default: keep the current one, or 161).",
            },
            "community_secret_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "description": (
                    "Vault path of the secret that holds the read-only community. It replaces "
                    "the current communities. Never put the community itself here."
                ),
            },
            "community_secret_mount": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "Vault KV version 2 mount of the secret (default 'secret').",
            },
            "community_secret_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "Key in the secret that holds the community (default 'community').",
            },
            "trap_targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "description": "Where traps go. Replaces the current list; omit to keep it.",
                "items": {
                    "type": "object",
                    "properties": {
                        "host_name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "pattern": _HOST_NAME_PATTERN,
                            "description": "Trap receiver: a host name or an IP address.",
                        },
                        "port": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 65535,
                            "default": 162,
                            "description": "UDP port of the receiver (default 162).",
                        },
                    },
                    "required": ["host_name"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["host", "enabled"],
        "additionalProperties": False,
        "examples": [
            {
                "host": "<host-name>",
                "enabled": True,
                "community_secret_ref": "<vault-path-of-the-community>",
                "trap_targets": [{"host_name": "trap-receiver.example.invalid"}],
            }
        ],
    },
    response=response_schema(_SNMP_STATE, ["community_required", "community_secret_unusable"]),
    llm={
        "when_to_call": (
            "Call to turn the SNMP agent of one ESXi host on or off, and set its port, its "
            "read-only community and its trap targets, like 'esxcli system snmp set'."
        ),
        "preconditions": (
            "Store the community in Vault first and pass its path; MEHO never takes it as a "
            "value. Fields left out keep their current value. Trap targets use the same "
            "community. To start the snmpd service, use host.service_control. The change "
            "waits for approval."
        ),
        "parameter_hints": {
            "community_secret_ref": "A Vault path; never the community itself.",
            "trap_targets": "e.g. [{'host_name': '<trap-receiver>', 'port': 162}].",
        },
        "output_shape": (
            "{status: applied|unchanged|community_required|community_secret_unusable|failed|"
            "not_verified|<host refusal>, host, requested, before and after: {enabled, port, "
            "read_only_community_count, trap_targets: [{host_name, port}]}, applied_steps, "
            "fault, guidance, issues}. No community string is ever returned."
        ),
        "next_step": NEXT_STEP_TAIL,
    },
)

# ---------------------------------------------------------------------------
# host.coredump.set
# ---------------------------------------------------------------------------

_PARTITION: Final = {
    "type": ["object", "null"],
    "description": "A diagnostic partition.",
    "properties": {
        "disk_name": {**NULL_STR, "description": "Disk of the partition."},
        "partition": {"type": ["integer", "null"], "description": "Partition number."},
        "storage_type": {**NULL_STR, "description": "Storage type, such as directAttached."},
        "diagnostic_type": {**NULL_STR, "description": "singleHost or multiHost."},
        "slots": {"type": ["integer", "null"], "description": "Number of dump slots."},
    },
}

COREDUMP_SET: Final[dict[str, Any]] = op_parts(
    parameters={
        "type": "object",
        "properties": {
            "host": HOST_PARAMETER,
            "disk_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "Disk of the partition, as in before.available of a call.",
            },
            "partition": {
                "type": "integer",
                "minimum": 1,
                "description": "Partition number on the disk.",
            },
        },
        "required": ["host", "disk_name", "partition"],
        "additionalProperties": False,
        "examples": [{"host": "<host-name>", "disk_name": "<disk-name>", "partition": 7}],
    },
    response=response_schema(
        {
            "type": ["object", "null"],
            "description": "The core dump partition and the free ones.",
            "properties": {
                "active": _PARTITION,
                "available": {
                    "type": "array",
                    "items": _PARTITION,
                    "description": "Free diagnostic partitions the host can use.",
                },
            },
        },
        ["partition_not_available"],
    ),
    llm={
        "when_to_call": (
            "Call to choose the core dump partition of one ESXi host, like 'esxcli system "
            "coredump partition set'. Only a partition target; a network dump server is not "
            "set here."
        ),
        "preconditions": (
            "The partition must be a free diagnostic partition of the host. Call with any "
            "value first to see the list in before.available (status partition_not_available, "
            "nothing sent). The change waits for approval."
        ),
        "parameter_hints": {"disk_name": "From before.available, e.g. '<disk-name>'."},
        "output_shape": (
            "{status: applied|unchanged|partition_not_available|failed|not_verified|<host "
            "refusal>, host, requested, before and after: {active: {disk_name, partition, "
            "storage_type, diagnostic_type, slots}, available: [...]}, applied_steps, fault, "
            "guidance, issues}."
        ),
        "next_step": NEXT_STEP_TAIL,
    },
)
