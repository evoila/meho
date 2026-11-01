# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the three VM device changes.

``vmware.composite.vm.disk.remove``, ``vmware.composite.vm.nic.remove`` and
``vmware.composite.vm.controller.add`` call vSphere REST and the VIM API. This
file checks those calls in two steps:

* **shape (always runs)** -- the calls each module declares match the frozen
  lists below;
* **existence (skips without the vendor-licensed shelf)** -- every REST call is
  served by the pinned ``vcenter.yaml`` of vCenter 8.0 and 9.0; every VIM
  method is served by the VI/JSON file of 8.0 U1, U2, U3 and 9.0, and the VIM
  types and the ``destroy`` file operation the bodies use are in each of those
  files; the REST create bodies use only fields the pinned files declare.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import (
    _vm_devices,
    _vm_disk_remove,
    _vm_nic_remove,
)
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_EXPECTED_NIC_REMOVE = {
    "GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
    "GET:/vcenter/vm/{vm}/hardware/ethernet",
    "DELETE:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
}
_EXPECTED_CONTROLLER_ADD_REST = {
    f"{method}:/vcenter/vm/{{vm}}/hardware/adapter/{kind}{suffix}"
    for kind in ("scsi", "sata", "nvme")
    for method, suffix in (("GET", ""), ("GET", "/{adapter}"), ("POST", ""))
}
_EXPECTED_VIM = {
    "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    "POST:/VirtualMachine/{moId}/ReconfigVM_Task",
}

#: The VIM types the request bodies carry (``_typeName``).
_VIM_TYPES = ("VirtualMachineConfigSpec", "VirtualDeviceConfigSpec", "VirtualUSBXHCIController")

#: The fields each REST create body may carry.
_CREATE_FIELDS = {
    "POST:/vcenter/vm/{vm}/hardware/adapter/scsi": {"type", "bus", "sharing"},
    "POST:/vcenter/vm/{vm}/hardware/adapter/sata": {"type", "bus"},
    "POST:/vcenter/vm/{vm}/hardware/adapter/nvme": {"bus"},
}

_REST_SHELVES = ["vcenter-8.0", "vcenter-9.0"]
_VI_JSON_FILES = [
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
    ("vcenter-9.0", "vi-json.yaml"),
]


def test_declared_calls_match_the_frozen_lists() -> None:
    """Shape gate (always runs)."""
    assert set(_vm_nic_remove.SUB_OPS_VM_NIC_REMOVE) == _EXPECTED_NIC_REMOVE
    assert set(_vm_devices.SUB_OPS_VM_CONTROLLER_ADD) == _EXPECTED_CONTROLLER_ADD_REST
    assert set(_vm_devices._VIM_SUB_OPS_VM_CONTROLLER_ADD) == _EXPECTED_VIM
    assert set(_vm_disk_remove._VIM_SUB_OPS_VM_DISK_REMOVE) == _EXPECTED_VIM


def test_the_bodies_use_the_types_this_lane_checks() -> None:
    """The USB add body names the VIM types listed above (always runs)."""
    body = _vm_devices._usb_add_body()
    change = body["spec"]["deviceChange"][0]
    used = {body["spec"]["_typeName"], change["_typeName"], change["device"]["_typeName"]}
    assert used == set(_VIM_TYPES)


@pytest.mark.parametrize("shelf", _REST_SHELVES)
def test_every_rest_call_is_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(
        _EXPECTED_NIC_REMOVE | _EXPECTED_CONTROLLER_ADD_REST,
        served,
        spec_label=f"{shelf}/vcenter.yaml",
    )


def test_the_rest_create_bodies_use_spec_fields() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    spec_fields = openapi_request_body_props(spec_path, spec_source="spec:vcenter.yaml")
    for op_id, fields in _CREATE_FIELDS.items():
        assert fields <= spec_fields[op_id], op_id


@pytest.mark.parametrize(("shelf", "filename"), _VI_JSON_FILES)
def test_every_vim_call_and_type_is_in_the_pinned_vi_json_spec(shelf: str, filename: str) -> None:
    spec_path = require_shelf_spec(shelf, filename)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_EXPECTED_VIM, served, spec_label=f"{shelf}/{filename}")
    text = spec_path.read_text(encoding="utf-8")
    for type_name in _VIM_TYPES:
        assert f"\n    {type_name}:\n" in text, f"{type_name} missing from {filename}"
    enum_at = text.index("\n    VirtualDeviceConfigSpecFileOperation_enum:\n")
    block = text[enum_at : text.index("\n\n    ", enum_at + 1)]
    values = {
        line.strip()[2:].strip("'\"")
        for line in block.splitlines()
        if line.strip().startswith("- ")
    }
    assert "destroy" in values
