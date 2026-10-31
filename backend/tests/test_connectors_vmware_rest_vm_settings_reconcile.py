# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the VM boot, upgrade and advanced-settings operations against the pinned API files.

* **Always runs:** the calls each module declares match the frozen lists below,
  so a change to them is seen.
* **Skips without the spec shelf, runs in CI:**

  * every REST call is served by the pinned ``vcenter.yaml`` of vCenter 8.0
    and 9.0;
  * the REST bodies MEHO sends use fields the pinned 9.0 ``vcenter.yaml``
    names (``Boot.UpdateSpec``, the boot device list, the two upgrade bodies);
  * every VIM method is a POST path of the pinned 9.0 ``vi-json.yaml``.

vCenter 8.0 U1 to U3 VI-JSON files are checked by the source scan of
``test_connectors_vmware_rest_8_0_spec_reconcile.py``, which finds these
methods in the connector source on its own.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest import typed_ops_vm_advanced_settings as adv
from meho_backplane.connectors.vmware_rest import typed_ops_vm_boot as boot
from meho_backplane.connectors.vmware_rest.composites import _vm_advanced_settings as adv_set
from meho_backplane.connectors.vmware_rest.composites import _vm_boot as boot_set
from meho_backplane.connectors.vmware_rest.composites import _vm_upgrade as upgrade
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_RETRIEVE = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
_RECONFIG = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"

_EXPECTED = {
    "boot_show_rest": {
        "GET:/vcenter/vm",
        "GET:/vcenter/vm/{vm}/hardware/boot",
        "GET:/vcenter/vm/{vm}/hardware/boot/device",
    },
    "boot_set": {
        "GET:/vcenter/vm/{vm}/hardware/boot",
        "GET:/vcenter/vm/{vm}/hardware/boot/device",
        "PATCH:/vcenter/vm/{vm}/hardware/boot",
        "PUT:/vcenter/vm/{vm}/hardware/boot/device",
        _RETRIEVE,
        _RECONFIG,
    },
    "hardware_upgrade": {
        "GET:/vcenter/vm/{vm}/hardware",
        "POST:/vcenter/vm/{vm}/hardware?action=upgrade",
    },
    "tools_upgrade": {
        "GET:/vcenter/vm/{vm}/tools",
        "POST:/vcenter/vm/{vm}/tools?action=upgrade",
        "GET:/vcenter/vm/{vm}/tools/installer",
        "POST:/vcenter/vm/{vm}/tools/installer?action=connect",
    },
    "advanced_settings_set": {_RETRIEVE, _RECONFIG},
}

_SHELVES = ["vcenter-8.0", "vcenter-9.0"]


def _rest_calls() -> set[str]:
    calls = (
        set(boot.BOOT_REST_CALLS)
        | set(adv.ADVANCED_SETTINGS_REST_CALLS)
        | set(boot_set.SUB_OPS_VM_BOOT_SET)
        | set(upgrade.SUB_OPS_VM_HARDWARE_UPGRADE)
        | set(upgrade.SUB_OPS_VM_TOOLS_UPGRADE)
    )
    return {call for call in calls if ":/vcenter/" in call}


def _vim_calls() -> set[str]:
    calls = (
        set(boot.BOOT_VIM_CALLS)
        | set(adv.ADVANCED_SETTINGS_VIM_CALLS)
        | set(boot_set.SUB_OPS_VM_BOOT_SET)
        | set(adv_set.VIM_SUB_OPS_VM_ADVANCED_SETTINGS_SET)
    )
    return {call for call in calls if ":/vcenter/" not in call}


def test_declared_calls_match_the_frozen_lists() -> None:
    assert set(boot.BOOT_REST_CALLS) == _EXPECTED["boot_show_rest"]
    assert set(boot.BOOT_VIM_CALLS) == {_RETRIEVE}
    assert set(adv.ADVANCED_SETTINGS_VIM_CALLS) == {_RETRIEVE}
    assert set(boot_set.SUB_OPS_VM_BOOT_SET) == _EXPECTED["boot_set"]
    assert set(upgrade.SUB_OPS_VM_HARDWARE_UPGRADE) == _EXPECTED["hardware_upgrade"]
    assert set(upgrade.SUB_OPS_VM_TOOLS_UPGRADE) == _EXPECTED["tools_upgrade"]
    assert set(adv_set.VIM_SUB_OPS_VM_ADVANCED_SETTINGS_SET) == _EXPECTED["advanced_settings_set"]
    assert _vim_calls() == {_RETRIEVE, _RECONFIG}


@pytest.mark.parametrize("shelf", _SHELVES)
def test_every_rest_call_is_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(_rest_calls(), served, spec_label=f"{shelf}/vcenter.yaml")


def test_rest_bodies_use_fields_of_the_pinned_9_0_vcenter_spec() -> None:
    """The 9.0 file only: the 8.0 file names its bodies through
    ``components/requestBodies`` and folds one-field bodies into a bare value,
    so the ingest parser reads no body fields from it."""
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    bodies = openapi_request_body_props(spec_path, spec_source="spec:vcenter.yaml")
    patch_fields = {spec_field for spec_field, _state in boot_set._PATCH_FIELDS.values()}
    assert patch_fields <= bodies["PATCH:/vcenter/vm/{vm}/hardware/boot"]
    assert bodies["PUT:/vcenter/vm/{vm}/hardware/boot/device"] == {"devices"}
    assert "version" in bodies["POST:/vcenter/vm/{vm}/hardware?action=upgrade"]
    assert "command_line_options" in bodies["POST:/vcenter/vm/{vm}/tools?action=upgrade"]


def _vi_json_post_paths(lines: list[str]) -> set[str]:
    """``POST:<path>`` of every path item with a ``post`` operation (line scan)."""
    found: set[str] = set()
    in_paths = False
    current: str | None = None
    for line in lines:
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_paths = line.rstrip() == "paths:"
            current = None
        elif in_paths and line.startswith("  ") and not line.startswith("   "):
            key = line.strip()
            current = key[:-1] if key.startswith("/") and key.endswith(":") else None
        elif current is not None and line.strip() == "post:":
            found.add(f"POST:{current}")
    return found


def test_every_vim_method_is_a_post_path_of_the_pinned_9_0_file() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    served = _vi_json_post_paths(spec_path.read_text(encoding="utf-8").splitlines())
    assert served, "vi-json.yaml parsed to zero POST paths"
    missing = sorted(_vim_calls() - served)
    assert not missing, f"the pinned 9.0 vi-json.yaml has no POST path for {missing}"
