# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VI-JSON shapes for the PCI passthrough changes of :mod:`._vm_pci_device`.

* :func:`passthrough_rows` turns a VM's ``config.hardware.device`` into flat
  rows (one per ``VirtualPCIPassthrough``).
* :func:`direct_backing` and :func:`dynamic_backing` pick the device the
  VM's host offers (``ConfigTarget.pciPassthrough`` and
  ``ConfigTarget.dynamicPassthrough``, from
  ``EnvironmentBrowser.QueryConfigTarget``) and build the backing:
  ``VirtualPCIPassthroughDeviceBackingInfo`` for DirectPath I/O, or
  ``VirtualPCIPassthroughDynamicBackingInfo`` for dynamic DirectPath.
* :func:`add_spec` and :func:`remove_spec` build the ``VirtualMachineConfigSpec``
  of the ``ReconfigVM_Task``.

No I/O here.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, unwrap_vim_value

__all__ = [
    "add_spec",
    "as_str",
    "direct_backing",
    "dynamic_backing",
    "passthrough_rows",
    "raw_passthrough",
    "remove_spec",
]

_PCI_PASSTHROUGH_TYPE: Final = "VirtualPCIPassthrough"
_DIRECT_BACKING_TYPE: Final = "VirtualPCIPassthroughDeviceBackingInfo"
_DYNAMIC_BACKING_TYPE: Final = "VirtualPCIPassthroughDynamicBackingInfo"
_ALLOWED_DEVICE_TYPE: Final = "VirtualPCIPassthroughAllowedDevice"
_DEVICE_CONFIG_SPEC_TYPE: Final = "VirtualDeviceConfigSpec"
_VM_CONFIG_SPEC_TYPE: Final = "VirtualMachineConfigSpec"
#: Most host devices named in a "not offered" answer.
_LIST_CAP: Final = 20


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_u16(value: Any) -> int | None:
    """A PCI id as an unsigned number; vCenter sends some as signed 16-bit values."""
    number = _as_int(value)
    return None if number is None else number % 0x10000


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def passthrough_rows(devices: Any) -> list[dict[str, Any]]:
    """The VM's PCI passthrough devices as flat rows."""
    rows: list[dict[str, Any]] = []
    for device in unwrap_vim_value(devices) or []:
        if not isinstance(device, dict) or device.get(VIM_TYPE_NAME_KEY) != _PCI_PASSTHROUGH_TYPE:
            continue
        backing = _as_dict(device.get("backing"))
        info = _as_dict(device.get("deviceInfo"))
        allowed = backing.get("allowedDevice") or []
        first = allowed[0] if allowed and isinstance(allowed[0], dict) else {}
        dynamic = backing.get(VIM_TYPE_NAME_KEY) == _DYNAMIC_BACKING_TYPE
        rows.append(
            {
                "device_key": _as_int(device.get("key")),
                "label": as_str(info.get("label")),
                "kind": "dynamic" if dynamic else "directpath",
                "pci_id": as_str(backing.get("assignedId" if dynamic else "id")),
                "vendor_id": _as_u16(first.get("vendorId") if dynamic else backing.get("vendorId")),
                "device_id": _as_u16(first.get("deviceId")) if dynamic else None,
                "device_name": as_str(backing.get("deviceName")) or None,
                "custom_label": as_str(backing.get("customLabel")),
            }
        )
    return rows


def raw_passthrough(devices: Any, device_key: int) -> dict[str, Any] | None:
    for device in unwrap_vim_value(devices) or []:
        if (
            isinstance(device, dict)
            and device.get(VIM_TYPE_NAME_KEY) == _PCI_PASSTHROUGH_TYPE
            and device.get("key") == device_key
        ):
            return device
    return None


def _offered_direct(config: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for entry in config.get("pciPassthrough") or []:
        pci = entry.get("pciDevice") if isinstance(entry, dict) else None
        if isinstance(pci, dict) and isinstance(pci.get("id"), str):
            out.append({**pci, "systemId": entry.get("systemId")})
    return out


def _offered_dynamic(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in config.get("dynamicPassthrough") or [] if isinstance(e, dict)]


def direct_backing(pci_id: str, config: dict[str, Any]) -> dict[str, Any] | str:
    """The DirectPath I/O backing for *pci_id*, or the reason it is not offered."""
    offered = _offered_direct(config)
    match = next((p for p in offered if p["id"].lower() == pci_id.lower()), None)
    if match is None or not isinstance(match.get("systemId"), str):
        ids = sorted(p["id"] for p in offered)[:_LIST_CAP]
        return (
            f"the VM's host offers no passthrough device {pci_id!r}. Devices it offers: "
            f"{ids or 'none'}. Enable passthrough for the device on the host first"
        )
    device_id = _as_int(match.get("deviceId")) or 0
    return {
        VIM_TYPE_NAME_KEY: _DIRECT_BACKING_TYPE,
        "deviceName": as_str(match.get("deviceName")) or "",
        "id": match["id"],
        # HostPciDevice.deviceId is a signed 16-bit value; the backing wants
        # it as the hex string of its unsigned form.
        "deviceId": format(device_id % 0x10000, "x"),
        "systemId": match["systemId"],
        "vendorId": _as_int(match.get("vendorId")) or 0,
    }


def dynamic_backing(wanted: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | str:
    """The dynamic DirectPath backing for *wanted*, or the reason it is not offered."""
    vendor_id = wanted["vendor_id"]
    device_id = wanted["device_id"]
    label = wanted.get("custom_label")
    offered = _offered_dynamic(config)
    match = next(
        (
            d
            for d in offered
            if d.get("vendorId") == vendor_id
            and d.get("deviceId") == device_id
            and (label is None or d.get("customLabel") == label)
        ),
        None,
    )
    if match is None:
        seen = sorted(
            {
                f"{d.get('vendorId')}:{d.get('deviceId')} {d.get('deviceName') or ''}"
                for d in offered
            }
        )[:_LIST_CAP]
        return (
            f"the VM's host offers no dynamic passthrough device with vendor id {vendor_id} "
            f"and device id {device_id}"
            + (f" and label {label!r}" if label is not None else "")
            + f". Devices it offers: {seen or 'none'}"
        )
    backing: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _DYNAMIC_BACKING_TYPE,
        "deviceName": "",
        "allowedDevice": [
            {VIM_TYPE_NAME_KEY: _ALLOWED_DEVICE_TYPE, "vendorId": vendor_id, "deviceId": device_id}
        ],
    }
    if label is not None:
        backing["customLabel"] = label
    return backing


def add_spec(backing: dict[str, Any], reserve_all_memory: bool) -> dict[str, Any]:
    spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE,
        "deviceChange": [
            {
                VIM_TYPE_NAME_KEY: _DEVICE_CONFIG_SPEC_TYPE,
                "operation": "add",
                "device": {VIM_TYPE_NAME_KEY: _PCI_PASSTHROUGH_TYPE, "key": -1, "backing": backing},
            }
        ],
    }
    if reserve_all_memory:
        spec["memoryReservationLockedToMax"] = True
    return spec


def remove_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """The ``VirtualMachineConfigSpec`` that removes the device *raw* (as read)."""
    return {
        VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE,
        "deviceChange": [
            {VIM_TYPE_NAME_KEY: _DEVICE_CONFIG_SPEC_TYPE, "operation": "remove", "device": raw}
        ],
    }
