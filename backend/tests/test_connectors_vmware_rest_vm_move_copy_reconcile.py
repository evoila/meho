# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check ``vm.relocate`` and ``vm.clone_from_vm`` against the pinned 9.0 VI-JSON file.

* An always-on test pins the two child-op lists, so a change to them is seen.
* The shelf tests read the pinned ``vcenter-9.0/vi-json.yaml``: every method is
  a POST path there, every request field the handlers send is a property of
  its vendor type, and the enum values they send exist. They skip when the
  spec shelf is not configured and run in CI.

vCenter 8.0 U1 to U3 are checked by the source scan of
``test_connectors_vmware_rest_8_0_spec_reconcile.py``, which finds these
methods in the connector source on its own.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _vm_clone_from_vm, _vm_relocate
from meho_backplane.connectors.vmware_rest.composites._vm_move_copy_schemas import (
    VM_RELOCATE_PARAMETER_SCHEMA,
)
from tests._spec_shelf import require_shelf_spec

_READ = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"


def _vi_json_lines() -> list[str]:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    return spec_path.read_text(encoding="utf-8").splitlines()


def _post_paths(lines: list[str]) -> set[str]:
    """``POST:<path>`` for every path item with a POST (a line scan of the 8 MB file)."""
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


def _schema_block(lines: list[str], schema: str) -> list[str]:
    """The lines of one component schema (its key sits at 4-space indent)."""
    out: list[str] = []
    inside = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if inside and line.startswith("    ") and not line.startswith("     "):
            break
        if inside:
            out.append(line)
    return out


def _properties(lines: list[str], schema: str) -> set[str]:
    """The property names of one component schema (8-space keys under ``properties``)."""
    return {
        line.strip()[:-1]
        for line in _schema_block(lines, schema)
        if line.startswith("        ") and not line.startswith("         ") and line.endswith(":")
    }


def _enum_values(lines: list[str], schema: str) -> set[str]:
    values: set[str] = set()
    in_enum = False
    for line in _schema_block(lines, schema):
        stripped = line.strip()
        if stripped == "enum:":
            in_enum = True
        elif in_enum and stripped.startswith("- "):
            values.add(stripped[2:].strip().strip("'\""))
        elif in_enum:
            in_enum = False
    return values


def test_child_op_lists_are_pinned() -> None:
    assert _vm_relocate._VIM_SUB_OPS_VM_RELOCATE == (
        _READ,
        "POST:/VirtualMachine/{moId}/RelocateVM_Task",
    )
    assert _vm_clone_from_vm._VIM_SUB_OPS_VM_CLONE_FROM_VM == (
        _READ,
        "POST:/VirtualMachine/{moId}/CloneVM_Task",
    )


@pytest.fixture(scope="module")
def vi_json() -> list[str]:
    return _vi_json_lines()


def test_every_method_is_a_post_path(vi_json: list[str]) -> None:
    declared = set(_vm_relocate._VIM_SUB_OPS_VM_RELOCATE) | set(
        _vm_clone_from_vm._VIM_SUB_OPS_VM_CLONE_FROM_VM
    )
    assert declared <= _post_paths(vi_json)


@pytest.mark.parametrize(
    ("schema", "fields"),
    [
        ("RelocateVMRequestType", {"spec", "priority"}),
        ("CloneVMRequestType", {"folder", "name", "spec"}),
        (
            "VirtualMachineRelocateSpec",
            {"datastore", "pool", "folder", "host", "disk", "diskMoveType"},
        ),
        ("VirtualMachineRelocateSpecDiskLocator", {"diskId", "datastore"}),
        ("VirtualMachineCloneSpec", {"location", "template", "powerOn", "snapshot"}),
        ("TaskInfo", {"descriptionId", "state", "entity"}),
    ],
)
def test_every_request_field_is_a_vendor_property(
    vi_json: list[str], schema: str, fields: set[str]
) -> None:
    assert fields <= _properties(vi_json, schema)


def test_the_enum_values_exist(vi_json: list[str]) -> None:
    priorities = set(VM_RELOCATE_PARAMETER_SCHEMA["properties"]["priority"]["enum"])
    assert priorities <= _enum_values(vi_json, "VirtualMachineMovePriority_enum")
    assert _vm_clone_from_vm._LINKED_DISK_MOVE_TYPE in _enum_values(
        vi_json, "VirtualMachineRelocateDiskMoveOptions_enum"
    )
    assert {"queued", "running"} <= _enum_values(vi_json, "TaskInfoState_enum")
