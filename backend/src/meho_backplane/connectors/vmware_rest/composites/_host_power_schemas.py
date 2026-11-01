# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter schemas, response schemas and agent guidance of the host power ops.

Five changes on one ESXi host, each waits for approval:

* ``host.maintenance.enter`` -- a vendor job: the start answers ``accepted``
  with the vCenter task id; ``vmware.task.info`` follows the task.
* ``host.reboot`` and ``host.shutdown`` -- wait for the vCenter task, then
  read the host again.
* ``host.standby.enter`` and ``host.standby.exit`` -- the same, for the
  power-saving standby state.

The handlers are in :mod:`._host_power`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    _HARD_FAILURE_ISSUES_PROPERTY,
    VSAN_MAINTENANCE_MODES,
)

__all__ = [
    "HOST_POWER_LLM_INSTRUCTIONS",
    "HOST_POWER_PARAMETER_SCHEMAS",
    "HOST_POWER_RESPONSE_SCHEMAS",
    "MAINTENANCE_ENTER_STATUSES",
    "OP_MAINTENANCE_ENTER",
    "OP_REBOOT",
    "OP_SHUTDOWN",
    "OP_STANDBY_ENTER",
    "OP_STANDBY_EXIT",
    "POWER_STATUSES",
    "STANDBY_TIMEOUT_DEFAULT",
]

_P: Final = "vmware.composite.host."
OP_MAINTENANCE_ENTER: Final = _P + "maintenance.enter"
OP_REBOOT: Final = _P + "reboot"
OP_SHUTDOWN: Final = _P + "shutdown"
OP_STANDBY_ENTER: Final = _P + "standby.enter"
OP_STANDBY_EXIT: Final = _P + "standby.exit"

#: Every status ``host.maintenance.enter`` can answer.
MAINTENANCE_ENTER_STATUSES: Final[tuple[str, ...]] = (
    "accepted",
    "already_running",
    "tasks_incomplete",
    "unchanged",
    "not_connected",
    "not_found",
    "invalid_request",
)
#: Every status the four waiting changes can answer.
POWER_STATUSES: Final[tuple[str, ...]] = (
    "done",
    "unchanged",
    "not_in_maintenance",
    "not_connected",
    "not_found",
    "invalid_request",
    "timeout",
)

#: Default and bounds of the vendor's standby ``timeoutSec`` (seconds).
STANDBY_TIMEOUT_DEFAULT: Final = 600
_STANDBY_TIMEOUT_MIN: Final = 60
_STANDBY_TIMEOUT_MAX: Final = 3600

_HOST_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": "Host moid, for example host-21 (vmware.host.usage lists the hosts).",
}
_FORCE_PARAM: Final[dict[str, Any]] = {
    "type": "boolean",
    "default": False,
    "description": (
        "When true, the host is changed even when it is not in maintenance mode, with "
        "running VMs on it. Default false: MEHO refuses a host that is not in maintenance mode."
    ),
}
_STANDBY_TIMEOUT_PARAM: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": _STANDBY_TIMEOUT_MIN,
    "maximum": _STANDBY_TIMEOUT_MAX,
    "default": STANDBY_TIMEOUT_DEFAULT,
    "description": (
        "Seconds vCenter waits for the host to reach the new state before it fails the "
        f"task ({_STANDBY_TIMEOUT_MIN}-{_STANDBY_TIMEOUT_MAX}, default "
        f"{STANDBY_TIMEOUT_DEFAULT})."
    ),
}

_MAINTENANCE_ENTER_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "host": _HOST_PARAM,
        "vsan_mode": {
            "type": "string",
            "enum": VSAN_MAINTENANCE_MODES,
            "default": "ensureObjectAccessibility",
            "description": (
                "What vSAN does with the host's data. ensureObjectAccessibility (default): "
                "keep every object reachable. evacuateAllData: move all data off the host. "
                "noAction: leave the data. Ignored on a host without vSAN."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": 86400,
            "default": 0,
            "description": (
                "Seconds after which vCenter fails the task if the host is not yet in "
                "maintenance mode. 0 (default): no limit."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "<host-id>", "vsan_mode": "ensureObjectAccessibility"}],
}


def _host_and(extra: dict[str, Any], example: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"host": _HOST_PARAM, **extra},
        "required": ["host"],
        "additionalProperties": False,
        "examples": [{"host": "<host-id>", **example}],
    }


HOST_POWER_PARAMETER_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    OP_MAINTENANCE_ENTER: _MAINTENANCE_ENTER_PARAMETERS,
    OP_REBOOT: _host_and({"force": _FORCE_PARAM}, {}),
    OP_SHUTDOWN: _host_and({"force": _FORCE_PARAM}, {}),
    OP_STANDBY_ENTER: _host_and(
        {
            "timeout_seconds": _STANDBY_TIMEOUT_PARAM,
            "evacuate_powered_off_vms": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, vCenter also moves the powered-off VMs off the host (in a "
                    "cluster with DRS). Default false: they stay registered on the host."
                ),
            },
        },
        {"timeout_seconds": STANDBY_TIMEOUT_DEFAULT},
    ),
    OP_STANDBY_EXIT: _host_and(
        {"timeout_seconds": _STANDBY_TIMEOUT_PARAM},
        {"timeout_seconds": STANDBY_TIMEOUT_DEFAULT},
    ),
}

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

_STATE_FIELDS: Final[dict[str, Any]] = {
    "host": {"type": "string", "description": "The host moid of the call."},
    "name": {"type": ["string", "null"], "description": "The host's name in vCenter."},
    "connection_state": {
        "type": ["string", "null"],
        "description": "connected, notResponding or disconnected.",
    },
    "power_state": {
        "type": ["string", "null"],
        "description": "poweredOn, poweredOff, standBy or unknown.",
    },
    "in_maintenance_mode": {
        "type": ["boolean", "null"],
        "description": "Whether the host is in maintenance mode.",
    },
    "standby_mode": {
        "type": ["string", "null"],
        "description": "entering, exiting, in or none; null when vCenter does not say.",
    },
    "ha_state": {
        "type": ["string", "null"],
        "description": "The vSphere HA state of the host; null outside an HA cluster.",
    },
}

_TASK_FIELD: Final[dict[str, Any]] = {
    "type": ["string", "null"],
    "description": "The vCenter task id; null when nothing was sent.",
}
_GUIDANCE_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "description": "What to do next, in plain words.",
}

_MAINTENANCE_ENTER_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": list(MAINTENANCE_ENTER_STATUSES),
            "description": (
                "accepted: vCenter took the job; it is not done yet. already_running: a "
                "maintenance-mode task for this host runs; nothing sent. tasks_incomplete: "
                "vCenter did not give the state of every recent task, so a running start "
                "cannot be ruled out; nothing sent. unchanged: already in maintenance mode. "
                "not_connected, not_found, invalid_request: nothing sent."
            ),
        },
        **_STATE_FIELDS,
        "vsan_mode": {"type": "string", "description": "The vSAN data option of the call."},
        "task": _TASK_FIELD,
        "status_read": {
            "type": ["object", "null"],
            "properties": {
                "op_id": {"type": "string", "description": "The read that follows the task."},
                "params": {
                    "type": "object",
                    "description": "The parameters of that read (the task id).",
                },
            },
            "description": "How to follow the task; null when there is no task.",
        },
        "guidance": _GUIDANCE_FIELD,
    },
    "required": ["status", "host", "task", "guidance"],
}


def _power_response(done_text: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "issues": _HARD_FAILURE_ISSUES_PROPERTY,
            "status": {
                "type": "string",
                "enum": list(POWER_STATUSES),
                "description": (
                    f"done: {done_text} unchanged: already in that state, nothing sent. "
                    "not_in_maintenance, not_connected, not_found, invalid_request: nothing "
                    "sent. timeout: MEHO stopped waiting; the task still runs."
                ),
            },
            **_STATE_FIELDS,
            "task": _TASK_FIELD,
            "guidance": _GUIDANCE_FIELD,
        },
        "required": ["status", "host", "task", "guidance"],
    }


HOST_POWER_RESPONSE_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    OP_MAINTENANCE_ENTER: _MAINTENANCE_ENTER_RESPONSE,
    OP_REBOOT: _power_response("vCenter sent the reboot; the host restarts now."),
    OP_SHUTDOWN: _power_response("vCenter sent the shutdown; the host powers off now."),
    OP_STANDBY_ENTER: _power_response("the host is in standby."),
    OP_STANDBY_EXIT: _power_response("the host left standby and runs again."),
}

# ---------------------------------------------------------------------------
# Agent guidance
# ---------------------------------------------------------------------------

_STATE_SHAPE: Final = (
    "host, name, connection_state, power_state, in_maintenance_mode, standby_mode, ha_state"
)
_POWER_SHAPE: Final = (
    "{status: done|unchanged|not_in_maintenance|not_connected|not_found|invalid_request|"
    f"timeout, {_STATE_SHAPE}, task, guidance, issues}}. The state fields are read after the "
    "task ended. A faulted task makes the call fail, with the fault type and text."
)
_HOST_HINT: Final = "Host moid, for example host-21."
_FORCE_HINT: Final = "true only when the host must go down with VMs running on it."

HOST_POWER_LLM_INSTRUCTIONS: Final[dict[str, dict[str, Any]]] = {
    OP_MAINTENANCE_ENTER: {
        "when_to_call": (
            "Call to put one ESXi host into maintenance mode, for example before patching or "
            "hardware work, with a vSAN data option. It does not move VMs: use "
            "host.evacuate when running VMs must move first. A second person must approve."
        ),
        "preconditions": (
            "The host must be connected. Running VMs block maintenance mode unless DRS moves "
            "them. A host already in maintenance mode answers unchanged; a running "
            "maintenance-mode task on the host answers already_running with its id. When "
            "the state of a recent task cannot be read, the answer is tasks_incomplete and "
            "nothing is sent."
        ),
        "parameter_hints": {
            "host": _HOST_HINT,
            "vsan_mode": "Keep the default unless all data must leave the host.",
            "timeout_seconds": "0 means vCenter waits without limit.",
        },
        "output_shape": (
            "{status: accepted|already_running|tasks_incomplete|unchanged|not_connected|"
            f"not_found|invalid_request, {_STATE_SHAPE}, vsan_mode, task, status_read: {{op_id, "
            "params}}, guidance}. accepted means vCenter started the task, not that the host "
            "is in maintenance mode."
        ),
        "next_step": (
            "Read status_read (vmware.task.info with the task id) until the state is success "
            "or error, then read the host with vmware.host.usage. Use async: true for the "
            "start, so a lost reply keeps the task id. After a lost reply, do not start "
            "again: read vmware.tasks.recent or the host first."
        ),
    },
    OP_REBOOT: {
        "when_to_call": (
            "Call to reboot one ESXi host, for example after patching. A second person must "
            "approve."
        ),
        "preconditions": (
            "The host must be connected and in maintenance mode, unless force is true. "
            "A reboot stops every VM still running on the host."
        ),
        "parameter_hints": {"host": _HOST_HINT, "force": _FORCE_HINT},
        "output_shape": _POWER_SHAPE,
        "next_step": (
            "On done, the host restarts and is notResponding for some minutes. Read it with "
            "vmware.host.usage until it is connected, then end maintenance mode with "
            "host.maintenance.exit. On timeout, follow the task with vmware.task.info."
        ),
    },
    OP_SHUTDOWN: {
        "when_to_call": (
            "Call to power off one ESXi host, for example before it is moved or removed. "
            "A second person must approve."
        ),
        "preconditions": (
            "The host must be connected and in maintenance mode, unless force is true. "
            "MEHO cannot power the host on again; that needs the server's management board."
        ),
        "parameter_hints": {"host": _HOST_HINT, "force": _FORCE_HINT},
        "output_shape": _POWER_SHAPE,
        "next_step": (
            "On done, the host goes down and vCenter shows it notResponding. On timeout, "
            "follow the task with vmware.task.info."
        ),
    },
    OP_STANDBY_ENTER: {
        "when_to_call": (
            "Call to put one ESXi host into standby to save power; vCenter can wake it "
            "later. A second person must approve."
        ),
        "preconditions": (
            "The host must be connected and able to wake by Wake-on-LAN or its management "
            "board. vCenter moves the running VMs off first (DRS). A host in standby "
            "answers unchanged."
        ),
        "parameter_hints": {
            "host": _HOST_HINT,
            "timeout_seconds": "How long vCenter waits for the host to go down.",
            "evacuate_powered_off_vms": "true to move the powered-off VMs too.",
        },
        "output_shape": _POWER_SHAPE,
        "next_step": (
            "On done, power_state is standBy. Wake the host with host.standby.exit. On "
            "timeout, follow the task with vmware.task.info."
        ),
    },
    OP_STANDBY_EXIT: {
        "when_to_call": (
            "Call to wake one ESXi host from standby so it can run VMs again. A second "
            "person must approve."
        ),
        "preconditions": (
            "The host must be in standby. A host that is powered on answers unchanged and "
            "nothing is sent."
        ),
        "parameter_hints": {
            "host": _HOST_HINT,
            "timeout_seconds": "How long vCenter waits for the host to come back.",
        },
        "output_shape": _POWER_SHAPE,
        "next_step": (
            "On done, power_state is poweredOn and DRS may move VMs onto the host. On "
            "timeout, follow the task with vmware.task.info."
        ),
    },
}
