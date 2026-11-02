# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the VM device changes.

Covers ``vmware.composite.vm.nic.connect``, ``vmware.composite.vm.serial_port.add``,
``vmware.composite.vm.cpu_memory.options.set`` and the ``connect`` action of
``vmware.composite.vm.device.cdrom``. Two steps, like
``test_connectors_vmware_rest_vm_hardware_reconcile.py``:

* **shape (always runs)** -- the calls each module declares match the frozen
  lists below;
* **vendor files (skips without the vendor-licensed shelf)** -- every REST call
  is served by the pinned ``vcenter.yaml`` of vCenter 8.0 and 9.0, the serial
  port body uses only fields of ``Serial.CreateSpec``, and every VIM type,
  ConfigSpec field and read property exists in the pinned ``vi-json.yaml``.
"""

from __future__ import annotations

import re

import pytest

from meho_backplane.connectors.vmware_rest.composites import _vm_cpu_memory_options as opts
from meho_backplane.connectors.vmware_rest.composites import _vm_nic_serial as nic_serial
from meho_backplane.connectors.vmware_rest.composites import _write
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_EXPECTED_NIC_CONNECT = {
    "GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
    "POST:/vcenter/vm/{vm}/hardware/ethernet/{nic}?action=connect",
    "POST:/vcenter/vm/{vm}/hardware/ethernet/{nic}?action=disconnect",
}
_EXPECTED_SERIAL_PORT_ADD = {
    "POST:/vcenter/vm/{vm}/hardware/serial",
    "GET:/vcenter/vm/{vm}/hardware/serial/{port}",
}
_CDROM_CONNECT = "POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=connect"

_SHELVES = ["vcenter-8.0", "vcenter-9.0"]


def _rest_calls() -> set[str]:
    return (
        set(nic_serial.SUB_OPS_VM_NIC_CONNECT)
        | set(nic_serial.SUB_OPS_VM_SERIAL_PORT_ADD)
        | set(_write._SUB_OPS_VM_DEVICE_CDROM)
    )


def test_declared_calls_match_the_frozen_lists() -> None:
    """Shape gate (always runs)."""
    assert set(nic_serial.SUB_OPS_VM_NIC_CONNECT) == _EXPECTED_NIC_CONNECT
    assert set(nic_serial.SUB_OPS_VM_SERIAL_PORT_ADD) == _EXPECTED_SERIAL_PORT_ADD
    assert _CDROM_CONNECT in _write._SUB_OPS_VM_DEVICE_CDROM


def test_cpu_memory_options_reuse_the_disk_grow_vim_pair() -> None:
    """The VIM calls are the pair ``vm.disk.grow`` already checks against vi-json.yaml."""
    assert set(opts.VIM_SUB_OPS_VM_CPU_MEMORY_OPTIONS_SET) == set(_write._VIM_SUB_OPS_VM_DISK_GROW)


@pytest.mark.parametrize("shelf", _SHELVES)
def test_every_rest_call_is_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    """Path existence (skips without the shelf)."""
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(_rest_calls(), served, spec_label=f"{shelf}/vcenter.yaml")


# Only the 9.0 file: the 8.0 file puts this body behind a shared
# ``requestBodies`` reference that the ingest parser does not follow. Its
# ``VcenterVmHardwareSerialCreateSpec`` has the same four fields.
@pytest.mark.parametrize("shelf", ["vcenter-9.0"])
@pytest.mark.parametrize(
    "params",
    [
        {"vm": "vm-1", "file": "[ds] a.log", "yield_on_poll": True, "allow_guest_control": True},
        {"vm": "vm-1", "network_location": "telnet://:1", "proxy": "telnet://p:1"},
    ],
)
def test_serial_body_uses_only_create_spec_fields(shelf: str, params: dict[str, object]) -> None:
    """The serial port body is a flat ``Serial.CreateSpec`` (skips without the shelf)."""
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    props = openapi_request_body_props(spec_path, spec_source="spec:vcenter.yaml")
    served = props["POST:/vcenter/vm/{vm}/hardware/serial"]
    assert set(nic_serial._serial_create_spec(params)) <= served


def _component_fields(spec_text: str, name: str) -> set[str]:
    """Property names of one component schema in vi-json.yaml (text scan, no YAML load)."""
    match = re.search(rf"\n    {re.escape(name)}:\n(.*?)(?=\n    [A-Za-z_])", spec_text, re.S)
    assert match is not None, f"{name} is not a component schema in vi-json.yaml"
    return set(re.findall(r"\n        ([A-Za-z_][A-Za-z0-9_]*):\n", match.group(1)))


def test_vim_types_fields_and_properties_are_in_the_pinned_vi_json() -> None:
    """Every VIM type, field and read property exists in vi-json.yaml (skips without it)."""
    spec_text = require_shelf_spec("vcenter-9.0", "vi-json.yaml").read_text(encoding="utf-8")
    spec_fields = _component_fields(spec_text, _write._VM_CONFIG_SPEC_TYPE)
    assert set(opts.HOT_ADD_FIELDS.values()) | {"cpuAllocation", "memoryAllocation"} <= spec_fields
    assert "shares" in _component_fields(spec_text, opts.RESOURCE_ALLOCATION_INFO_TYPE)
    assert {"level", "shares"} <= _component_fields(spec_text, opts.SHARES_INFO_TYPE)
    config_info = _component_fields(spec_text, "VirtualMachineConfigInfo")
    runtime_info = _component_fields(spec_text, "VirtualMachineRuntimeInfo")
    for prop in opts.READ_PROPERTIES:
        head, _, tail = prop.partition(".")
        if head == "config":
            assert tail in config_info, prop
        elif head == "runtime":
            assert tail in runtime_info, prop
        else:
            assert prop == "name"
