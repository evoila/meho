# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract texts of the vCenter VM and folder multi-step operations.

Checks what these operations tell the AI, without calling vCenter:

* every example under ``examples`` validates against its parameter schema
  (JSON Schema Draft 2020-12, the check MEHO runs before each call);
* the "library item id or name" rule of ``vm.deploy_from_library`` and
  ``vm.import_from_library`` is a ``oneOf``, so a call with both or neither
  fails before the call reaches the handler;
* the ``power_states`` filter of ``vm.power.bulk`` takes only vCenter power
  states, as one string or as a list;
* every op has ``llm_instructions`` with the keys the AI reads.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import schemas
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.operations._validate import validate_params

_LIBRARY_ITEM_ID = "aaaaaaaa-0000-0000-0000-000000000001"

_PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    "vm.clone": schemas.VM_CLONE_PARAMETER_SCHEMA,
    "vm.clone_from_template": schemas.VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA,
    "vm.create": schemas.VM_CREATE_PARAMETER_SCHEMA,
    "vm.deploy_from_library": schemas.VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA,
    "vm.destroy": schemas.VM_DESTROY_PARAMETER_SCHEMA,
    "vm.device.cdrom": schemas.VM_DEVICE_CDROM_PARAMETER_SCHEMA,
    "vm.disk.attach": schemas.VM_DISK_ATTACH_PARAMETER_SCHEMA,
    "vm.disk.grow": schemas.VM_DISK_GROW_PARAMETER_SCHEMA,
    "vm.import_from_library": schemas.VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
    "vm.migrate": schemas.VM_MIGRATE_PARAMETER_SCHEMA,
    "vm.nic.repoint": schemas.VM_NIC_REPOINT_PARAMETER_SCHEMA,
    "vm.power": schemas.VM_POWER_PARAMETER_SCHEMA,
    "vm.power.bulk": schemas.VM_POWER_BULK_PARAMETER_SCHEMA,
    "vm.resize": schemas.VM_RESIZE_PARAMETER_SCHEMA,
    "vm.resource_allocation.set": schemas.VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA,
    "vm.resource_allocation.show": schemas.VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA,
    "vm.snapshot.revert": schemas.VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
    "vm.snapshot.create": schemas.VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA,
    "vm.snapshot.delete": schemas.VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA,
    "vm.snapshot.delete_all": schemas.VM_SNAPSHOT_DELETE_ALL_PARAMETER_SCHEMA,
    "vm.disks.consolidate": schemas.VM_DISKS_CONSOLIDATE_PARAMETER_SCHEMA,
    "folder.create": schemas.FOLDER_CREATE_PARAMETER_SCHEMA,
    "folder.delete": schemas.FOLDER_DELETE_PARAMETER_SCHEMA,
}

_READ_ONLY = {"vm.resource_allocation.show"}


@pytest.mark.parametrize("short_id", sorted(_PARAMETER_SCHEMAS))
def test_every_example_validates(short_id: str) -> None:
    schema = _PARAMETER_SCHEMAS[short_id]
    Draft202012Validator.check_schema(schema)
    examples = schema.get("examples")
    assert isinstance(examples, list) and examples, f"{short_id} has no example"
    for example in examples:
        assert validate_params(schema, example) == [], (short_id, example)


@pytest.mark.parametrize("short_id", sorted(_PARAMETER_SCHEMAS))
def test_llm_instructions_have_the_keys_the_ai_reads(short_id: str) -> None:
    guidance = VM_LLM_INSTRUCTIONS[f"vmware.composite.{short_id}"]
    keys = {"when_to_call", "output_shape", "next_step"}
    if short_id not in _READ_ONLY:
        keys.add("preconditions")
    assert keys <= set(guidance), (short_id, keys - set(guidance))
    assert "when_to_use" not in guidance


def test_llm_instructions_cover_only_ops_in_this_table() -> None:
    assert set(VM_LLM_INSTRUCTIONS) == {f"vmware.composite.{s}" for s in _PARAMETER_SCHEMAS}


_LIBRARY_SCHEMAS = {
    "vm.deploy_from_library": (schemas.VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA, {}),
    "vm.import_from_library": (
        schemas.VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
        {"datastore": "datastore-15"},
    ),
}


@pytest.mark.parametrize("short_id", sorted(_LIBRARY_SCHEMAS))
@pytest.mark.parametrize(
    ("item", "accepted"),
    [
        ({"library_item": _LIBRARY_ITEM_ID}, True),
        ({"library_item_name": "<item-name>"}, True),
        ({"library_item": _LIBRARY_ITEM_ID, "library_item_name": "<item-name>"}, False),
        ({}, False),
    ],
    ids=["id-only", "name-only", "both", "neither"],
)
def test_library_item_is_id_or_name_never_both(
    short_id: str, item: dict[str, str], accepted: bool
) -> None:
    schema, extra = _LIBRARY_SCHEMAS[short_id]
    params = {"resource_pool": "resgroup-8", **extra, **item}
    errors = validate_params(schema, params)
    assert (errors == []) is accepted, errors


@pytest.mark.parametrize(
    ("power_states", "accepted"),
    [
        ("POWERED_OFF", True),
        (["POWERED_OFF", "SUSPENDED"], True),
        ("invalid-state", False),
        (["POWERED_ON", "invalid-state"], False),
    ],
    ids=["string", "list", "bad-string", "bad-list-item"],
)
def test_bulk_power_state_filter_takes_only_vcenter_states(
    power_states: str | list[str], accepted: bool
) -> None:
    params = {"filter": {"power_states": power_states}, "action": "start"}
    errors = validate_params(schemas.VM_POWER_BULK_PARAMETER_SCHEMA, params)
    assert (errors == []) is accepted, errors
