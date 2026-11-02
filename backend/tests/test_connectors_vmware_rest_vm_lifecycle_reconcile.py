# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the VM template, register and instant clone calls against the pinned vendor files.

The five changes call these vendor methods and paths:

* VI-JSON: ``VirtualMachine.MarkAsTemplate``, ``VirtualMachine.MarkAsVirtualMachine``,
  ``Folder.RegisterVM_Task``, ``VirtualMachine.UnregisterVM`` and the VM read
  ``PropertyCollector.RetrievePropertiesEx``;
* REST: ``POST /api/vcenter/vm?action=instant-clone``.

An always-on test pins the child-op lists, so a change to them is seen. The
shelf tests read the pinned files: every VI-JSON method is a POST path of the
9.0 ``vi-json.yaml`` and the request fields MEHO sends exist there; the REST
path is served by the 9.0 and the 8.0 ``vcenter.yaml`` with the body fields
MEHO sends. They skip when the spec shelf is not configured and run in CI.
vCenter 8.0 U1 to U3 VI-JSON files are checked by the source scan of
``test_connectors_vmware_rest_8_0_spec_reconcile.py``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _vm_instant_clone, _vm_lifecycle
from meho_backplane.connectors.vmware_rest.composites._vm_register import register_body
from meho_backplane.connectors.vmware_rest.composites._vm_template import unmark_body
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_RETRIEVE = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

_VIM_MANIFESTS = {
    "_VIM_SUB_OPS_VM_TEMPLATE_MARK": (_RETRIEVE, "POST:/VirtualMachine/{moId}/MarkAsTemplate"),
    "_VIM_SUB_OPS_VM_TEMPLATE_UNMARK": (
        _RETRIEVE,
        "POST:/VirtualMachine/{moId}/MarkAsVirtualMachine",
    ),
    "_VIM_SUB_OPS_VM_REGISTER": (_RETRIEVE, "POST:/Folder/{moId}/RegisterVM_Task"),
    "_VIM_SUB_OPS_VM_UNREGISTER": (_RETRIEVE, "POST:/VirtualMachine/{moId}/UnregisterVM"),
    "_VIM_SUB_OPS_VM_INSTANT_CLONE": (_RETRIEVE,),
}


def test_child_op_lists_are_the_expected_sets() -> None:
    for name, expected in _VIM_MANIFESTS.items():
        assert getattr(_vm_lifecycle, name) == expected, name
    assert _vm_instant_clone._SUB_OPS_VM_INSTANT_CLONE == ("POST:/vcenter/vm?action=instant-clone",)


def _vi_json_lines() -> list[str]:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    return spec_path.read_text(encoding="utf-8").splitlines()


def _post_paths(lines: list[str]) -> set[str]:
    """``POST:<path>`` for every path item with a ``post`` operation (line scan)."""
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


def _schema_properties(lines: list[str], schema: str) -> set[str]:
    """The property names of one component schema (4-space schema key, 8-space properties)."""
    names: set[str] = set()
    inside = in_props = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("    ") and not line.startswith("     ") and line.strip():
            break
        if line == "      properties:":
            in_props = True
        elif in_props and line.startswith("      ") and not line.startswith("       "):
            in_props = False
        elif in_props and line.startswith("        ") and not line.startswith("         "):
            names.add(line.strip().rstrip(":"))
    return names


def test_vim_methods_are_post_paths_of_the_pinned_9_0_file() -> None:
    served = _post_paths(_vi_json_lines())
    assert served, "vi-json.yaml parsed to zero POST paths"
    declared = {op for ops in _VIM_MANIFESTS.values() for op in ops}
    missing = sorted(declared - served)
    assert not missing, f"the pinned 9.0 vi-json.yaml has no POST path for {missing}"


def test_vim_request_fields_exist_in_the_pinned_9_0_file() -> None:
    lines = _vi_json_lines()
    register = register_body(
        {
            "vmx_path": "[ds] a/a.vmx",
            "name": "a",
            "as_template": False,
            "resource_pool": "resgroup-1",
            "host": "host-1",
        }
    )
    assert set(register) <= _schema_properties(lines, "RegisterVMRequestType")
    unmark = unmark_body({"resource_pool": "resgroup-1", "host": "host-1"})
    assert set(unmark) <= _schema_properties(lines, "MarkAsVirtualMachineRequestType")


def _clone_body_fields() -> set[str]:
    body = _vm_instant_clone.instant_clone_body(
        {
            "source_vm": "vm-1",
            "name": "a",
            "folder": "group-v1",
            "resource_pool": "resgroup-1",
            "datastore": "datastore-1",
            "disconnect_all_nics": True,
        }
    )
    return set(body)


@pytest.mark.parametrize("product_dir", ["vcenter-9.0", "vcenter-8.0"])
def test_instant_clone_path_is_served(product_dir: str) -> None:
    spec_path = require_shelf_spec(product_dir, "vcenter.yaml")
    assert_op_ids_served(
        [_vm_instant_clone._OP_INSTANT_CLONE],
        openapi_served_op_ids(spec_path),
        spec_label=f"{product_dir}/vcenter.yaml",
    )


def test_instant_clone_body_fields_are_in_the_9_0_spec() -> None:
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    served = openapi_request_body_props(spec_path)[_vm_instant_clone._OP_INSTANT_CLONE]
    assert _clone_body_fields() <= served, sorted(_clone_body_fields() - served)


def test_instant_clone_body_fields_are_in_the_8_0_spec() -> None:
    """The 8.0 file keeps the body behind a reference the parser does not follow."""
    lines = require_shelf_spec("vcenter-8.0", "vcenter.yaml").read_text("utf-8").splitlines()
    served = _schema_properties(lines, "VcenterVMInstantCloneSpec")
    assert _clone_body_fields() <= served, sorted(_clone_body_fields() - served)
