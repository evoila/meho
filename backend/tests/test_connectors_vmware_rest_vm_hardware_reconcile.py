# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the VM hardware read and the two cdrom / Tools changes.

``vmware.vm.hardware.show``, ``vmware.composite.vm.cdrom.add`` and
``vmware.composite.vm.tools.installer.unmount`` call vSphere REST directly.
This file checks those calls in two steps, like
``test_connectors_vmware_rest_sso_admin_api_file_reconcile.py``:

* **shape (always runs)** -- the calls each module declares match the frozen
  lists below;
* **path existence (skips without the vendor-licensed shelf)** -- every call
  is served by the pinned ``vcenter.yaml`` of vCenter 8.0 and 9.0.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest import typed_ops_vm_hardware as hw
from meho_backplane.connectors.vmware_rest.composites import _vm_cdrom_tools as tools
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_EXPECTED_READ = {
    "GET:/vcenter/vm",
    "GET:/vcenter/vm/{vm}/hardware/cpu",
    "GET:/vcenter/vm/{vm}/hardware/memory",
    "GET:/vcenter/vm/{vm}/hardware/cdrom",
    "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
    "GET:/vcenter/vm/{vm}/hardware/adapter/scsi",
    "GET:/vcenter/vm/{vm}/hardware/adapter/scsi/{adapter}",
    "GET:/vcenter/vm/{vm}/hardware/adapter/sata",
    "GET:/vcenter/vm/{vm}/hardware/adapter/sata/{adapter}",
    "GET:/vcenter/vm/{vm}/hardware/adapter/nvme",
    "GET:/vcenter/vm/{vm}/hardware/adapter/nvme/{adapter}",
    "GET:/vcenter/vm/{vm}/tools/installer",
}
_EXPECTED_CDROM_ADD = {
    "GET:/vcenter/vm/{vm}/hardware/cdrom",
    "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
    "POST:/vcenter/vm/{vm}/hardware/cdrom",
}
_EXPECTED_INSTALLER_UNMOUNT = {
    "GET:/vcenter/vm/{vm}/tools/installer",
    "POST:/vcenter/vm/{vm}/tools/installer?action=disconnect",
}

_SHELVES = ["vcenter-8.0", "vcenter-9.0"]


def _all_calls() -> set[str]:
    return (
        set(hw.HARDWARE_REST_CALLS)
        | set(tools.SUB_OPS_VM_CDROM_ADD)
        | set(tools.SUB_OPS_VM_TOOLS_INSTALLER_UNMOUNT)
    )


def test_declared_calls_match_the_frozen_lists() -> None:
    """Shape gate (always runs)."""
    assert set(hw.HARDWARE_REST_CALLS) == _EXPECTED_READ
    assert set(tools.SUB_OPS_VM_CDROM_ADD) == _EXPECTED_CDROM_ADD
    assert set(tools.SUB_OPS_VM_TOOLS_INSTALLER_UNMOUNT) == _EXPECTED_INSTALLER_UNMOUNT


@pytest.mark.parametrize("shelf", _SHELVES)
def test_every_call_is_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    """Path existence (skips without the shelf)."""
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(_all_calls(), served, spec_label=f"{shelf}/vcenter.yaml")
