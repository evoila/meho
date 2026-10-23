# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the vCenter alarm operations against the pinned 9.0 VI-JSON file (#4383).

The alarm read and the two alarm changes call these VI-JSON methods:
``ServiceInstance.RetrieveServiceContent``, ``PropertyCollector.RetrievePropertiesEx``,
``AlarmManager.AcknowledgeAlarm`` and ``AlarmManager.ClearTriggeredAlarms``.

* An always-on test pins the two child-op lists, so a change to them is seen.
* The shelf tests read the pinned ``vcenter-9.0/vi-json.yaml``: every method is
  a POST path there, the vendor enum values the reset filter sends exist, and
  every object type the list accepts has ``triggeredAlarmState``. They skip
  when the spec shelf is not configured and run in CI.

vCenter 8.0 U1 to U3 are checked by the source scan of
``test_connectors_vmware_rest_8_0_spec_reconcile.py``, which finds these
methods in the connector source on its own.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.alarm_state import ALARM_ENTITY_TYPES
from meho_backplane.connectors.vmware_rest.composites import _alarm
from tests._spec_shelf import require_shelf_spec


def _vi_json_lines() -> list[str]:
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    return spec_path.read_text(encoding="utf-8").splitlines()


def _paths_with(lines: list[str], method: str) -> set[str]:
    """``<METHOD>:<path>`` for every path item with that operation.

    A line scan instead of a YAML parse of the 8 MB file: a path key sits at
    2-space indent, its operations at 4-space indent (the same scan as the
    8.0 lane).
    """
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
        elif current is not None and line.strip() == f"{method.lower()}:":
            found.add(f"{method}:{current}")
    return found


def _enum_values(lines: list[str], schema: str) -> set[str]:
    """The ``enum`` items of one component schema (4-space schema key)."""
    values: set[str] = set()
    inside = False
    in_enum = False
    for line in lines:
        if line == f"    {schema}:":
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("    ") and not line.startswith("     "):
            break
        stripped = line.strip()
        if stripped == "enum:":
            in_enum = True
        elif in_enum and stripped.startswith("- "):
            values.add(stripped[2:].strip().strip("'\""))
        elif in_enum:
            in_enum = False
    return values


def test_alarm_child_op_lists_are_the_expected_sets() -> None:
    assert _alarm._VIM_SUB_OPS_ALARM_ACKNOWLEDGE == (
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/AlarmManager/{moId}/AcknowledgeAlarm",
    )
    assert _alarm._VIM_SUB_OPS_ALARM_RESET == (
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/AlarmManager/{moId}/ClearTriggeredAlarms",
    )


def test_alarm_methods_are_post_paths_of_the_pinned_9_0_file() -> None:
    lines = _vi_json_lines()
    served = _paths_with(lines, "POST")
    assert served, "vi-json.yaml parsed to zero POST paths"
    declared = set(_alarm._VIM_SUB_OPS_ALARM_ACKNOWLEDGE) | set(_alarm._VIM_SUB_OPS_ALARM_RESET)
    missing = sorted(declared - served)
    assert not missing, f"the pinned 9.0 vi-json.yaml has no POST path for {missing}"


def test_reset_filter_values_are_vendor_enum_values() -> None:
    lines = _vi_json_lines()
    by_entity = _enum_values(lines, "AlarmFilterSpecAlarmTypeByEntity_enum")
    by_trigger = _enum_values(lines, "AlarmFilterSpecAlarmTypeByTrigger_enum")
    statuses = _enum_values(lines, "ManagedEntityStatus_enum")
    assert by_entity, "AlarmFilterSpecAlarmTypeByEntity_enum not found"
    assert set(_alarm._OBJECT_KIND_VENDOR.values()) <= by_entity
    assert set(_alarm._TRIGGER_KIND_VENDOR.values()) <= by_trigger
    assert {"red", "yellow"} <= statuses
    assert f"    {_alarm._ALARM_FILTER_SPEC_TYPE}:" in lines


def test_every_listable_object_type_has_triggered_alarm_state() -> None:
    lines = _vi_json_lines()
    served = _paths_with(lines, "GET")
    missing = sorted(
        mo_type
        for mo_type in ALARM_ENTITY_TYPES
        if f"GET:/{mo_type}/{{moId}}/triggeredAlarmState" not in served
    )
    assert not missing, f"no triggeredAlarmState in the pinned 9.0 vi-json.yaml for {missing}"
