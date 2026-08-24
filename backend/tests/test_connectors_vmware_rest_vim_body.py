# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the VI-JSON response un-boxer ``unwrap_vim_value`` (#3106).

VI-JSON boxes what lands in an ``Any`` placeholder (``DynamicProperty.val``,
``TaskInfo.result``): a primitive arrives as ``{"_typeName": "string",
"_value": "dvportgroup-1766"}`` (live-observed on vCenter 8.0.3) and an
array as ``{"_typeName": "ArrayOfString", "_value": [...]}`` -- every
``PrimitiveX`` / ``ArrayOfX`` component of the pinned ``vi-json.yaml`` keys
its payload ``_value`` -- while MoRefs / DataObjects arrive as plain
``_typeName``-annotated dicts. These tests pin the un-boxer's contract:

* primitive boxes (string / int / bool) unwrap to the bare value;
* ``ArrayOf*`` boxes unwrap to the bare list -- both the spec'd
  ``_value``-keyed form and the SOAP-flavoured element-keyed variant;
* plain values, MoRefs, and annotated DataObjects pass through unchanged
  (tolerant both ways -- un-boxed 9.x / vcsim payloads keep working);
* the walk recurses so boxes in nested ``Any`` positions normalise too.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value

# ---------------------------------------------------------------------------
# Primitive boxes (the live 8.0.3 evidence shapes, #3106)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("boxed", "expected"),
    [
        # The byte-shaped live evidence from #3106: vm.create's DVPG key read.
        ({"_typeName": "string", "_value": "dvportgroup-1766"}, "dvportgroup-1766"),
        ({"_typeName": "int", "_value": 42}, 42),
        ({"_typeName": "long", "_value": 17179869184}, 17179869184),
        ({"_typeName": "boolean", "_value": True}, True),
        ({"_typeName": "boolean", "_value": False}, False),
    ],
)
def test_primitive_boxes_unwrap_to_bare_values(boxed: dict[str, Any], expected: Any) -> None:
    assert unwrap_vim_value(boxed) == expected


def test_boxed_none_value_unwraps_to_none() -> None:
    assert unwrap_vim_value({"_typeName": "boolean", "_value": None}) is None


# ---------------------------------------------------------------------------
# ArrayOf* boxes
# ---------------------------------------------------------------------------


def test_array_box_value_keyed_unwraps_to_list() -> None:
    """The pinned-spec shape: every ``ArrayOfX`` keys its payload ``_value``."""
    assert unwrap_vim_value({"_typeName": "ArrayOfString", "_value": ["a", "b"]}) == ["a", "b"]


def test_array_box_element_keyed_variant_unwraps_to_list() -> None:
    """The SOAP-flavoured element-keyed variant the #3106 report cites is tolerated."""
    assert unwrap_vim_value({"_typeName": "ArrayOfString", "string": ["a", "b"]}) == ["a", "b"]


def test_array_box_of_dataobjects_unwraps_and_keeps_element_tags() -> None:
    """``ArrayOfVirtualDevice`` unwraps to the device list; element ``_typeName`` survives.

    The disk-grow handler keys the ``VirtualDisk`` pick on the element's
    ``_typeName`` tag, so the un-boxer must strip only the box.
    """
    disk = {"_typeName": "VirtualDisk", "key": 2000, "capacityInBytes": 10737418240}
    boxed = {"_typeName": "ArrayOfVirtualDevice", "_value": [disk]}
    assert unwrap_vim_value(boxed) == [disk]


def test_array_box_of_morefs_unwraps_to_plain_moref_list() -> None:
    """``TaskManager.recentTask`` arrives as a boxed MoRef array on live 8.0.x."""
    morefs = [
        {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-1"},
        {"_typeName": "ManagedObjectReference", "type": "Task", "value": "task-2"},
    ]
    assert unwrap_vim_value({"_typeName": "ArrayOfManagedObjectReference", "_value": morefs}) == (
        morefs
    )


def test_array_of_anytype_elements_unbox_individually() -> None:
    """``ArrayOfAnyType`` elements are ``Any`` themselves -- nested boxes normalise."""
    boxed = {
        "_typeName": "ArrayOfAnyType",
        "_value": ["bare", {"_typeName": "string", "_value": "boxed"}],
    }
    assert unwrap_vim_value(boxed) == ["bare", "boxed"]


# ---------------------------------------------------------------------------
# Plain values pass through (tolerant both ways)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plain",
    [
        "dvportgroup-1766",
        42,
        True,
        None,
        ["a", "b"],
        {},
        {"_typeName": "ManagedObjectReference", "type": "VirtualMachine", "value": "vm-9"},
        {"type": "VirtualMachine", "value": "vm-9"},  # un-annotated MoRef (vcsim)
    ],
)
def test_plain_values_pass_through_unchanged(plain: Any) -> None:
    assert unwrap_vim_value(plain) == plain


def test_annotated_dataobject_keeps_its_type_tag_and_fields() -> None:
    """A DataObject is not a box -- ``_typeName`` and every field survive."""
    snapshot_info = {
        "_typeName": "VirtualMachineSnapshotInfo",
        "rootSnapshotList": [
            {
                "_typeName": "VirtualMachineSnapshotTree",
                "name": "pre-upgrade",
                "snapshot": {
                    "_typeName": "ManagedObjectReference",
                    "type": "VirtualMachineSnapshot",
                    "value": "snapshot-5",
                },
                "childSnapshotList": [],
            }
        ],
    }
    assert unwrap_vim_value(snapshot_info) == snapshot_info


# ---------------------------------------------------------------------------
# Recursion through nested Any positions
# ---------------------------------------------------------------------------


def test_nested_boxes_inside_a_dataobject_normalise_in_one_pass() -> None:
    """A ``TaskInfo`` with boxed ``state`` / ``progress`` and a plain MoRef result."""
    moref = {"_typeName": "ManagedObjectReference", "type": "VirtualMachine", "value": "vm-88"}
    info = {
        "_typeName": "TaskInfo",
        "state": {"_typeName": "string", "_value": "success"},
        "progress": {"_typeName": "int", "_value": 100},
        "result": moref,
    }
    assert unwrap_vim_value(info) == {
        "_typeName": "TaskInfo",
        "state": "success",
        "progress": 100,
        "result": moref,
    }
