# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract texts of the vCenter Supervisor, namespace, content library and
guest customization multi-step operations.

Checks what these operations tell the AI, without calling vCenter:

* every example under ``examples`` validates against its parameter schema
  (JSON Schema Draft 2020-12, the check MEHO runs before each call);
* the new enums refuse a wrong value before the call reaches the handler;
* every op has ``llm_instructions`` with the keys the AI reads.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import schemas
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.operations._validate import validate_params

_PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    "supervisor.enable": schemas.SUPERVISOR_ENABLE_PARAMETER_SCHEMA,
    "supervisor.disable": schemas.SUPERVISOR_DISABLE_PARAMETER_SCHEMA,
    "supervisor.status": schemas.SUPERVISOR_STATUS_PARAMETER_SCHEMA,
    "namespace.create": schemas.NAMESPACE_CREATE_PARAMETER_SCHEMA,
    "namespace.delete": schemas.NAMESPACE_DELETE_PARAMETER_SCHEMA,
    "namespace.status": schemas.NAMESPACE_STATUS_PARAMETER_SCHEMA,
    "content_library.delete": schemas.CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA,
    "content_library.item.delete": schemas.CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA,
    "content_library.subscribed.create": (
        schemas.CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA
    ),
    "content_library.subscribed.items.list": (
        schemas.CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA
    ),
    "content_library.subscribed.status": (
        schemas.CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA
    ),
    "content_library.subscribed.sync": schemas.CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA,
    "guest.customization_spec.create": (schemas.GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA),
    "vm.customize": schemas.VM_CUSTOMIZE_PARAMETER_SCHEMA,
}

_READ_ONLY = {
    "supervisor.status",
    "namespace.status",
    "content_library.subscribed.items.list",
    "content_library.subscribed.status",
}


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
    guidance = SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[f"vmware.composite.{short_id}"]
    keys = {"when_to_call", "output_shape", "next_step"}
    if short_id not in _READ_ONLY:
        keys.add("preconditions")
    assert keys <= set(guidance), (short_id, keys - set(guidance))
    assert "when_to_use" not in guidance


def test_llm_instructions_cover_only_ops_in_this_table() -> None:
    expected = {f"vmware.composite.{s}" for s in _PARAMETER_SCHEMAS}
    assert set(SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS) == expected


def _enable_example() -> dict[str, Any]:
    return copy.deepcopy(schemas.SUPERVISOR_ENABLE_PARAMETER_SCHEMA["examples"][0])


@pytest.mark.parametrize("size", ["TINY", "SMALL", "MEDIUM", "LARGE"])
def test_supervisor_enable_accepts_every_control_plane_size(size: str) -> None:
    params = _enable_example()
    params["control_plane"]["size"] = size
    assert validate_params(schemas.SUPERVISOR_ENABLE_PARAMETER_SCHEMA, params) == []


@pytest.mark.parametrize("size", ["HUGE", "small", ""])
def test_supervisor_enable_refuses_a_wrong_size_before_the_call(size: str) -> None:
    params = _enable_example()
    params["control_plane"]["size"] = size
    errors = validate_params(schemas.SUPERVISOR_ENABLE_PARAMETER_SCHEMA, params)
    assert [(e["path"], e["validator"]) for e in errors] == [("$.control_plane.size", "enum")]


@pytest.mark.parametrize(
    ("field", "value"),
    [("subject_type", "ROBOT"), ("role", "ADMIN")],
)
def test_namespace_create_refuses_a_wrong_access_value_before_the_call(
    field: str, value: str
) -> None:
    params = copy.deepcopy(schemas.NAMESPACE_CREATE_PARAMETER_SCHEMA["examples"][0])
    params["access_list"][0][field] = value
    errors = validate_params(schemas.NAMESPACE_CREATE_PARAMETER_SCHEMA, params)
    assert errors, (field, value)
