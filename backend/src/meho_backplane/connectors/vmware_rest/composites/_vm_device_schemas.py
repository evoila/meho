# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the VM device changes.

Used by :mod:`._vm_nic_serial` (network adapter connect, serial port add) and
:mod:`._vm_cpu_memory_options` (CPU and memory hot add and shares). Kept apart
so each handler module stays small.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import VM_MOID_PATTERN

#: A vCenter device id: digits only (``4000`` for a network adapter).
DEVICE_ID_PATTERN: Final = r"^[0-9]+$(?!\n)"

_VM_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VM_MOID_PATTERN,
    "description": (
        "VM id, such as 'vm-42'. Get it from vmware.vm.info or vmware.vm.list. A change "
        "takes no VM name, so after approval it runs on the VM the approver saw."
    ),
}
_NULLABLE_STR: Final = {"type": ["string", "null"]}
_NULLABLE_BOOL: Final = {"type": ["boolean", "null"]}

VM_NIC_CONNECT_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID_PROPERTY,
        "nic": {
            "type": "string",
            "pattern": DEVICE_ID_PATTERN,
            "description": "Network adapter id, such as '4000'. vmware.vm.info lists them.",
        },
        "action": {
            "type": "string",
            "enum": ["connect", "disconnect"],
            "default": "connect",
            "description": "connect plugs the adapter in; disconnect unplugs it. Default connect.",
        },
    },
    "required": ["vm", "nic"],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "nic": "4000", "action": "connect"},
        {"vm": "vm-42", "nic": "4000", "action": "disconnect"},
    ],
}

_NIC_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "label": {**_NULLABLE_STR, "description": "Adapter label."},
        "mac_address": {**_NULLABLE_STR, "description": "MAC address."},
        "state": {
            **_NULLABLE_STR,
            "description": "Connection state, such as CONNECTED or NOT_CONNECTED.",
        },
        "start_connected": {**_NULLABLE_BOOL, "description": "Connect at power on."},
        "network": {**_NULLABLE_STR, "description": "Network id of the backing."},
        "network_name": {**_NULLABLE_STR, "description": "Network name of the backing."},
    },
}

VM_NIC_CONNECT_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["connected", "disconnected", "unchanged", "not_found", "refused"],
            "description": (
                "connected / disconnected: done; unchanged: already so, nothing sent; "
                "not_found: no such adapter; refused: vCenter said no, nothing changed."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "nic": {"type": "string", "description": "Network adapter id."},
        "action": {"type": "string", "enum": ["connect", "disconnect"], "description": "Asked."},
        "before": {**_NIC_ROW_SCHEMA, "description": "The adapter before the change."},
        "after": {**_NIC_ROW_SCHEMA, "description": "The adapter read again after it."},
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm", "nic", "action"],
}

VM_SERIAL_PORT_ADD_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID_PROPERTY,
        "file": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^\[[^\]]+\] \S.*$(?!\n)",
            "description": (
                "Datastore path of the file the port writes to, such as "
                "'[datastore1] logs/serial.log'. Give this or network_location."
            ),
        },
        "network_location": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^[a-z][a-z0-9+.-]*://\S*$(?!\n)",
            "description": (
                "Network address as a URI, such as 'telnet://:<port>' for a server on the "
                "VM's host. Give this or file."
            ),
        },
        "network_role": {
            "type": "string",
            "enum": ["server", "client"],
            "default": "server",
            "description": (
                "With network_location only. server: the port listens; client: the VM "
                "connects to that address. Default server."
            ),
        },
        "proxy": {
            "type": "string",
            "minLength": 1,
            "description": "With network_location only. URI of a proxy service, if one is used.",
        },
        "start_connected": {
            "type": "boolean",
            "description": "Connect the port when the VM powers on. Default true.",
        },
        "yield_on_poll": {
            "type": "boolean",
            "description": "Let the VM give up the CPU while it only polls the port.",
        },
        "allow_guest_control": {
            "type": "boolean",
            "description": "Let the guest connect and disconnect the port. Default false.",
        },
    },
    "required": ["vm"],
    "oneOf": [{"required": ["file"]}, {"required": ["network_location"]}],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "file": "[<datastore-name>] <folder>/serial.log"},
        {"vm": "vm-42", "network_location": "telnet://:<port>", "network_role": "server"},
    ],
}

VM_SERIAL_PORT_ADD_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["added", "refused", "invalid_request"],
            "description": (
                "added: the port exists; refused: vCenter said no, nothing changed; "
                "invalid_request: bad input, nothing sent."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "requested": {
            "type": ["object", "null"],
            "description": "The Serial.CreateSpec MEHO sent.",
        },
        "port": {**_NULLABLE_STR, "description": "Id of the new serial port."},
        "port_info": {
            "type": ["object", "null"],
            "description": "The new port as read back, or null.",
            "properties": {
                "label": {**_NULLABLE_STR, "description": "Port label."},
                "backing_type": {
                    **_NULLABLE_STR,
                    "description": "FILE, NETWORK_SERVER or NETWORK_CLIENT.",
                },
                "file": {**_NULLABLE_STR, "description": "Datastore path of the file."},
                "network_location": {**_NULLABLE_STR, "description": "Network URI."},
                "state": {**_NULLABLE_STR, "description": "Connection state."},
                "start_connected": {**_NULLABLE_BOOL, "description": "Connect at power on."},
                "yield_on_poll": {**_NULLABLE_BOOL, "description": "Yield on poll."},
                "allow_guest_control": {**_NULLABLE_BOOL, "description": "Guest control."},
            },
        },
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_NIC_CONNECT_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to plug a running VM's network adapter in (connect) or unplug it "
        "(disconnect), for example to cut a VM off the network and bring it back. To "
        "move the adapter to another network, use vmware.composite.vm.nic.repoint."
    ),
    "preconditions": (
        "The VM must be powered on; vCenter refuses the call for a VM that is off. Read "
        "the VM first with vmware.vm.info to get its id (vm) and the adapter id (nic). "
        "The change takes the VM id, not a name. It waits for approval."
    ),
    "parameter_hints": {"nic": "Adapter id, such as '4000'.", "action": "connect or disconnect."},
    "output_shape": (
        "{status: connected|disconnected|unchanged|not_found|refused, vm, nic, action, "
        "before: {label, mac_address, state, start_connected, network, network_name}, "
        "after: {...}, error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: when the adapter is already in the asked state, nothing is "
        "sent. On refused, read guidance (often the VM is off). Check after.state."
    ),
}

VM_SERIAL_PORT_ADD_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to give a VM a serial port that writes to a file on a datastore, or that "
        "listens on or connects to a network address (a serial console)."
    ),
    "preconditions": (
        "The VM must be powered off; vCenter refuses the call for a running VM. Get the "
        "VM id (vm) from vmware.vm.info. Give exactly one of file or network_location. A "
        "network server port opens a port on the VM's host. It waits for approval."
    ),
    "parameter_hints": {
        "file": "'[<datastore-name>] <folder>/<file>.log'.",
        "network_location": "'telnet://:<port>' for a server; a full URI for a client.",
    },
    "output_shape": (
        "{status: added|refused|invalid_request, vm, requested, port, port_info: {label, "
        "backing_type, file, network_location, state, start_connected, ...}, error_type, "
        "guidance}."
    ),
    "next_step": (
        "Not safe to run twice: a second call adds a second port. On refused, read "
        "guidance (often the VM is on), power the VM off and try again."
    ),
}

_SHARES_LEVEL: Final[dict[str, Any]] = {
    "type": "string",
    "enum": ["low", "normal", "high", "custom"],
}
_SHARES_COUNT: Final[dict[str, Any]] = {"type": "integer", "minimum": 1}

VM_CPU_MEMORY_OPTIONS_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM_ID_PROPERTY,
        "cpu_hot_add_enabled": {
            "type": "boolean",
            "description": "Turn CPU hot add on (true) or off (false). Needs the VM powered off.",
        },
        "memory_hot_add_enabled": {
            "type": "boolean",
            "description": "Turn memory hot add on or off. Needs the VM powered off.",
        },
        "cpu_shares_level": {
            **_SHARES_LEVEL,
            "description": "CPU shares: low, normal, high, or custom (then give cpu_shares).",
        },
        "cpu_shares": {
            **_SHARES_COUNT,
            "description": "Number of CPU shares. Only with cpu_shares_level custom.",
        },
        "memory_shares_level": {
            **_SHARES_LEVEL,
            "description": "Memory shares: low, normal, high, or custom (then memory_shares).",
        },
        "memory_shares": {
            **_SHARES_COUNT,
            "description": "Number of memory shares. Only with memory_shares_level custom.",
        },
    },
    "required": ["vm"],
    "anyOf": [
        {"required": ["cpu_hot_add_enabled"]},
        {"required": ["memory_hot_add_enabled"]},
        {"required": ["cpu_shares_level"]},
        {"required": ["memory_shares_level"]},
    ],
    "dependentRequired": {
        "cpu_shares": ["cpu_shares_level"],
        "memory_shares": ["memory_shares_level"],
    },
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "cpu_hot_add_enabled": True, "memory_hot_add_enabled": True},
        {"vm": "vm-42", "cpu_shares_level": "custom", "cpu_shares": 4000},
    ],
}

_SHARES_VIEW: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "level": {**_NULLABLE_STR, "description": "low, normal, high or custom."},
        "shares": {"type": ["integer", "null"], "description": "Number of shares."},
    },
}

_SETTINGS_VIEW: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "cpu_hot_add_enabled": {**_NULLABLE_BOOL, "description": "CPU hot add on."},
        "memory_hot_add_enabled": {**_NULLABLE_BOOL, "description": "Memory hot add on."},
        "cpu_shares": {**_SHARES_VIEW, "description": "CPU shares."},
        "memory_shares": {**_SHARES_VIEW, "description": "Memory shares."},
    },
}

VM_CPU_MEMORY_OPTIONS_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "set",
                "unchanged",
                "requires_power_off",
                "invalid_request",
                "vm_not_found",
                "partial",
                "timeout",
            ],
            "description": (
                "set: done; unchanged: already so, nothing sent; requires_power_off: a hot "
                "add change on a VM that is not off, nothing sent; invalid_request / "
                "vm_not_found: nothing sent; partial: done, but not all read back; "
                "timeout: the task did not finish in time."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "name": {**_NULLABLE_STR, "description": "VM name."},
        "power_state": {**_NULLABLE_STR, "description": "poweredOn, poweredOff or suspended."},
        "requested": {
            "type": "object",
            "description": "The settings asked for, in the shape of before and after.",
        },
        "before": {**_SETTINGS_VIEW, "description": "The settings before the change."},
        "after": {**_SETTINGS_VIEW, "description": "The settings read again after it."},
        "task": {**_NULLABLE_STR, "description": "The vCenter task id."},
        "task_state": {**_NULLABLE_STR, "description": "success or timeout."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
        "issues": {
            "type": "array",
            "description": "Only on timeout: an error entry, so the call is recorded as failed.",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Where it went wrong."},
                    "severity": {"type": "string", "description": "error, warning or info."},
                    "message": {"type": "string", "description": "What went wrong."},
                },
            },
        },
    },
    "required": ["status", "vm"],
}

VM_CPU_MEMORY_OPTIONS_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to turn CPU or memory hot add on or off, or to set a VM's CPU or memory "
        "shares (its weight when the host is busy). To change the CPU count or memory "
        "size, use vmware.composite.vm.resize. For limits and reservations, use "
        "vmware.composite.vm.resource_allocation.set."
    ),
    "preconditions": (
        "Get the VM id (vm) from vmware.vm.info. A hot add change needs the VM powered "
        "off; shares change on a running VM too. custom shares need the number of shares. "
        "The change takes the VM id, not a name. It waits for approval."
    ),
    "parameter_hints": {
        "cpu_shares_level": "low, normal, high or custom.",
        "cpu_shares": "Only with custom, such as 4000.",
    },
    "output_shape": (
        "{status: set|unchanged|requires_power_off|invalid_request|vm_not_found|partial|"
        "timeout, vm, name, power_state, requested, before: {cpu_hot_add_enabled, "
        "memory_hot_add_enabled, cpu_shares: {level, shares}, memory_shares}, after, task, "
        "task_state, guidance}. A failed task is an error, not a status."
    ),
    "next_step": (
        "Safe to run twice: settings that are already so are not sent. On "
        "requires_power_off, power the VM off with vmware.composite.vm.power and call again."
    ),
}
