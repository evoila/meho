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

from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_message,
    fault_type_name,
    method_fault_type,
    unwrap_vim_value,
)

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


# ---------------------------------------------------------------------------
# fault_message: both TaskInfo.error wire shapes (#3116 nested / #3663 flat)
# ---------------------------------------------------------------------------


def test_fault_message_flattened_8_0_x_prefixes_type_name_on_faultstring() -> None:
    """8.0.x VI-JSON: the concrete fault is flattened onto ``error`` (#3663).

    ``_typeName`` + ``faultstring`` sit directly on ``error`` with no
    ``LocalizedMethodFault`` wrapper and no nested ``fault`` -- the shape a
    live vCenter 8.0.3 ``CreateVM_Task`` rejection returns. The extractor
    surfaces the type name together with the faultstring text so the
    operator sees both, instead of the historical ``<no fault reported>``.
    """
    error = {
        "_typeName": "InvalidArgument",
        "faultstring": "A specified parameter was not correct: configSpec.guestId",
        "invalidProperty": "configSpec.guestId",
    }
    assert (
        fault_message(error)
        == "InvalidArgument: A specified parameter was not correct: configSpec.guestId"
    )


def test_fault_message_flattened_8_0_x_no_text_reports_type_name() -> None:
    """A flattened fault with a type but no text at all must not crash -- it
    surfaces the concrete ``_typeName`` (#3663)."""
    assert fault_message({"_typeName": "TaskInProgress"}) == "TaskInProgress"


def test_fault_message_flattened_8_0_x_faultstring_without_type_name() -> None:
    """A flattened fault carrying only ``faultstring`` surfaces the bare text."""
    assert fault_message({"faultstring": "boom"}) == "boom"


def test_fault_message_flattened_8_0_x_joins_fault_messages_with_type_name() -> None:
    """A flattened fault whose text lives in ``faultMessage`` joins the texts
    and prefixes the concrete ``_typeName`` (#3663)."""
    error = {
        "_typeName": "InvalidArgument",
        "faultMessage": [
            {"_typeName": "LocalizableMessage", "message": "The argument is invalid."},
            {"_typeName": "LocalizableMessage", "message": "Entity crosses datacenter boundary."},
        ],
    }
    assert fault_message(error) == (
        "InvalidArgument: The argument is invalid.; Entity crosses datacenter boundary."
    )


def test_fault_message_9_x_localized_message_is_returned_verbatim() -> None:
    """9.x ``LocalizedMethodFault.localizedMessage`` surfaces verbatim, with no
    type-name prefix -- the historical shape is preserved (#3116)."""
    error = {
        "_typeName": "LocalizedMethodFault",
        "localizedMessage": "The disk cannot be shrunk.",
        "fault": {"_typeName": "InvalidArgument"},
    }
    assert fault_message(error) == "The disk cannot be shrunk."


def test_fault_message_9_x_joins_nested_fault_messages_verbatim() -> None:
    """9.x nested ``fault.faultMessage[*].message`` joins verbatim (no prefix, #3116)."""
    error = {
        "_typeName": "LocalizedMethodFault",
        "fault": {
            "_typeName": "InvalidArgument",
            "faultMessage": [
                {"_typeName": "LocalizableMessage", "message": "The argument is invalid."},
                {
                    "_typeName": "LocalizableMessage",
                    "message": "Entity crosses datacenter boundary.",
                },
            ],
        },
    }
    assert fault_message(error) == "The argument is invalid.; Entity crosses datacenter boundary."


def test_fault_message_9_x_falls_back_to_nested_type_name() -> None:
    """9.x nested fault with no message text surfaces the nested concrete
    ``_typeName`` -- never the wrapper's ``LocalizedMethodFault`` (#3116)."""
    error = {
        "_typeName": "LocalizedMethodFault",
        "fault": {"_typeName": "TaskInProgress"},
    }
    assert fault_message(error) == "TaskInProgress"


def test_fault_message_non_dict_and_empty_return_none() -> None:
    """No usable fault content -- non-dict, empty dict, or a non-dict ``fault``
    -- returns ``None`` (the callers' ``<no fault reported>`` path)."""
    assert fault_message(None) is None
    assert fault_message("boom") is None
    assert fault_message({}) is None
    assert fault_message({"fault": "not-a-dict"}) is None


# ---------------------------------------------------------------------------
# fault_type_name: never report the 9.x wrapper as the fault class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fault",
    [
        # Nested fault with no _typeName at all.
        {"_typeName": "LocalizedMethodFault", "fault": {"faultstring": "boom"}},
        # Nested fault whose _typeName is blank.
        {"_typeName": "LocalizedMethodFault", "fault": {"_typeName": "  "}},
        # Wrapper with no nested fault.
        {"_typeName": "LocalizedMethodFault", "localizedMessage": "boom"},
    ],
)
def test_fault_type_name_degenerate_9_x_wrapper_returns_none(fault: dict[str, Any]) -> None:
    """A 9.x wrapper without a usable nested class name gives ``None``, not
    ``LocalizedMethodFault`` -- the wrapper is not a fault class."""
    assert fault_type_name(fault) is None


def test_fault_type_name_reads_both_wire_shapes() -> None:
    """The nested 9.x class and the flattened 8.0.x class are both returned."""
    assert (
        fault_type_name(
            {"_typeName": "LocalizedMethodFault", "fault": {"_typeName": "NoPermission"}}
        )
        == "NoPermission"
    )
    assert fault_type_name({"_typeName": "InvalidProperty"}) == "InvalidProperty"
    assert fault_type_name(None) is None


# ---------------------------------------------------------------------------
# method_fault_type: the VI-JSON 500 fault-body parser (#3566)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '{"_typeName":"NotAuthenticated","object":{"type":"GuestProcessManager",'
            '"value":"guestOperationsProcessManager"},"privilegeId":"",'
            '"faultstring":"The session is not authenticated."}',
            "NotAuthenticated",
        ),
        ('{"_typeName":"InvalidLogin","faultstring":"x"}', "InvalidLogin"),
        ('{"_typeName":"NoPermission","privilegeId":"System.View"}', "NoPermission"),
        ('{"_typeName":"InvalidArgument","faultstring":"bad"}', "InvalidArgument"),
        # Not a fault body: empty, not JSON, not an object, no / blank _typeName.
        ("", None),
        ("<html>500 Internal Server Error</html>", None),
        ('["NotAuthenticated"]', None),
        ('"NotAuthenticated"', None),
        ('{"faultstring":"no type"}', None),
        ('{"_typeName":"  "}', None),
        ('{"_typeName":7}', None),
    ],
)
def test_method_fault_type_truth_table(body: str, expected: str | None) -> None:
    assert method_fault_type(body) == expected
