# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared reads and the reconfigure step of the VM security changes.

Three changes use this module: ``vm.vtpm.add`` and ``vm.vtpm.remove``
(:mod:`._vm_vtpm`) and ``vm.encryption.set`` (:mod:`._vm_encryption`).
vCenter's REST API has no call for them, so all three use the VIM API as
VI-JSON, like the other VM changes in this package: read the VM
(``RetrievePropertiesEx``), run one ``VirtualMachine.ReconfigVM_Task``
through the governed write seam (:func:`._write._write_vmomi_sub_op`), poll
the task with :func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`
and read the VM again to return its new state.

All three need the VM powered off; the handlers refuse a running VM before
any write. The VM is named by its id only: after approval the change must run
on the VM the approver saw. The answers carry no key material: only whether
a part is encrypted and the id of the key provider.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import STATUS_TIMEOUT
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _VM_CONFIG_SPEC_TYPE,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult

__all__ = [
    "disk_encrypted",
    "disks",
    "encryption_state",
    "hw_version",
    "power_state",
    "powered_on_problem",
    "read_vm",
    "reconfigure",
    "timeout_fields",
    "vm_name",
    "vtpm_device",
    "vtpm_view",
]

#: vi-json sub-op manifest of all three changes: the VM read (before, after and
#: the task poll) and the one reconfigure. The ``_VIM_SUB_OPS_*`` name keeps the
#: vcenter.yaml sweep off it; the reconcile lane checks it against vi-json.yaml.
_VIM_SUB_OPS_VM_SECURITY: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

#: The VIM data object types the request bodies carry (``_typeName``).
VIRTUAL_TPM_TYPE: Final = "VirtualTPM"
VIRTUAL_DISK_TYPE: Final = "VirtualDisk"
DEFINED_PROFILE_SPEC_TYPE: Final = "VirtualMachineDefinedProfileSpec"
BACKING_SPEC_TYPE: Final = "VirtualDeviceConfigSpecBackingSpec"
CRYPTO_SPEC_DECRYPT_TYPE: Final = "CryptoSpecDecrypt"

_PROP_POWER_STATE: Final = "runtime.powerState"
_PROP_DEVICES: Final = "config.hardware.device"
PROP_VERSION: Final = "config.version"
PROP_FIRMWARE: Final = "config.firmware"
_PROP_KEY_ID: Final = "config.keyId"
PROP_SNAPSHOT: Final = "snapshot"
_VM_PROPS: Final = [
    "name",
    _PROP_POWER_STATE,
    _PROP_DEVICES,
    PROP_VERSION,
    PROP_FIRMWARE,
    _PROP_KEY_ID,
    PROP_SNAPSHOT,
]

_POWERED_OFF: Final = "poweredOff"
_HW_VERSION_RE: Final = re.compile(r"^vmx-(\d+)$")

#: Wall-clock bound for the ReconfigVM_Task poll -- the 600 s convention;
#: module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_NOT_FOUND: Final = "not_found"
STATUS_NOT_CONFIRMED: Final = "not_confirmed"


def _devices(props: dict[str, Any]) -> list[dict[str, Any]]:
    devices = props.get(_PROP_DEVICES)
    return [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []


def vtpm_device(props: dict[str, Any]) -> dict[str, Any] | None:
    return next((d for d in _devices(props) if d.get(VIM_TYPE_NAME_KEY) == VIRTUAL_TPM_TYPE), None)


def disks(props: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in _devices(props) if d.get(VIM_TYPE_NAME_KEY) == VIRTUAL_DISK_TYPE]


def _label(device: dict[str, Any]) -> str | None:
    info = device.get("deviceInfo")
    label = info.get("label") if isinstance(info, dict) else None
    return label if isinstance(label, str) else None


def disk_encrypted(disk: dict[str, Any]) -> bool:
    backing = disk.get("backing")
    return isinstance(backing, dict) and isinstance(backing.get("keyId"), dict)


def vtpm_view(device: dict[str, Any] | None) -> dict[str, Any] | None:
    """The vTPM as the answer shows it: key and label only (no certificates)."""
    if device is None:
        return None
    return {"key": device.get("key"), "label": _label(device)}


def encryption_state(props: dict[str, Any]) -> dict[str, Any]:
    """``{vm_home_encrypted, key_provider, has_vtpm, disks}`` -- ids only, no key data."""
    key_id = props.get(_PROP_KEY_ID)
    provider = key_id.get("providerId") if isinstance(key_id, dict) else None
    provider_id = provider.get("id") if isinstance(provider, dict) else None
    return {
        "vm_home_encrypted": isinstance(key_id, dict),
        "key_provider": provider_id if isinstance(provider_id, str) else None,
        "has_vtpm": vtpm_device(props) is not None,
        "disks": [
            {"disk": str(d.get("key")), "label": _label(d), "encrypted": disk_encrypted(d)}
            for d in disks(props)
        ],
    }


def hw_version(props: dict[str, Any]) -> int | None:
    version = props.get(PROP_VERSION)
    match = _HW_VERSION_RE.match(version) if isinstance(version, str) else None
    return int(match.group(1)) if match else None


def vm_name(props: dict[str, Any]) -> str | None:
    name = props.get("name")
    return name if isinstance(name, str) else None


def power_state(props: dict[str, Any]) -> str | None:
    state = props.get(_PROP_POWER_STATE)
    return state if isinstance(state, str) else None


async def read_vm(
    connector: VmwareRestConnector, target: Any, operator: Operator, vm: str
) -> dict[str, Any] | None:
    """The VM's properties, or ``None`` when it does not exist.

    :func:`read_object` fails the call when vCenter could not read a property,
    so a missing permission never reads as "no vTPM" or "not encrypted".
    """
    return await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=_VM_PROPS
    )


def powered_on_problem(props: dict[str, Any]) -> str | None:
    state = power_state(props)
    if state == _POWERED_OFF:
        return None
    return f"the VM is {state or 'in an unknown power state'}; power it off first"


async def reconfigure(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    spec: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, VimTaskResult | None]:
    """Gate + issue one ``ReconfigVM_Task`` on ``params["vm"]`` and poll it.

    A parked or denied gate comes back as ``(gate, None)`` with nothing sent.
    A task fault raises (``connector_error``, audited as failed) with the
    fault type and text.
    """
    vm = params["vm"]
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/ReconfigVM_Task",
        body={"spec": {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE, **spec}},
        params=params,
    )
    if gate is not None:
        return gate, None
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"{params['op']}: ReconfigVM_Task on vm {vm!r} faulted "
            f"({outcome.fault_type or 'unknown fault'}): "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    return None, outcome


def timeout_fields(outcome: VimTaskResult, reread: str) -> dict[str, Any]:
    return {
        "status": STATUS_TIMEOUT,
        "task": outcome.task,
        "task_state": outcome.state,
        "guidance": (
            f"ReconfigVM_Task {outcome.task} did not finish within "
            f"{int(_TASK_TIMEOUT_SECONDS)}s; it may still finish. {reread}"
        ),
        "issues": [_task_timeout_issue("ReconfigVM_Task", outcome.task)],
    }
