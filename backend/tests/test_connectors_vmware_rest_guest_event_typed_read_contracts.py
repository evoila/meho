# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract texts of the vCenter guest, event, performance and typed read operations.

Checks what these operations tell the AI, without calling vCenter:

* every example under ``examples`` validates against its parameter schema
  (JSON Schema Draft 2020-12, the check MEHO runs before each call);
* every op has ``llm_instructions`` with the keys the AI reads, and no old
  ``when_to_use`` / ``result_shape`` key;
* every response schema names the fields of its lists, also nested ones.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vmware_rest.composites import schemas
from meho_backplane.connectors.vmware_rest.composites._guest_event_perf_llm_instructions import (
    GUEST_EVENT_PERF_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.operations._validate import validate_params

_COMPOSITES: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "event.tail": (
        schemas.EVENT_TAIL_PARAMETER_SCHEMA,
        schemas.EVENT_TAIL_RESPONSE_SCHEMA,
        "safe",
    ),
    "performance.summary": (
        schemas.PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
        schemas.PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
        "safe",
    ),
    "vm.guest.env.read": (
        schemas.GUEST_ENV_READ_PARAMETER_SCHEMA,
        schemas.GUEST_ENV_READ_RESPONSE_SCHEMA,
        "safe",
    ),
    "vm.guest.file.read": (
        schemas.GUEST_FILE_READ_PARAMETER_SCHEMA,
        schemas.GUEST_FILE_READ_RESPONSE_SCHEMA,
        "caution",
    ),
    "vm.guest.file.write": (
        schemas.GUEST_FILE_WRITE_PARAMETER_SCHEMA,
        schemas.GUEST_FILE_WRITE_RESPONSE_SCHEMA,
        "dangerous",
    ),
    "vm.guest.net.show": (
        schemas.GUEST_NET_SHOW_PARAMETER_SCHEMA,
        schemas.GUEST_NET_SHOW_RESPONSE_SCHEMA,
        "safe",
    ),
    "vm.guest.process.list": (
        schemas.GUEST_PROCESS_LIST_PARAMETER_SCHEMA,
        schemas.GUEST_PROCESS_LIST_RESPONSE_SCHEMA,
        "safe",
    ),
    "vm.guest.program.run": (
        schemas.GUEST_PROGRAM_RUN_PARAMETER_SCHEMA,
        schemas.GUEST_PROGRAM_RUN_RESPONSE_SCHEMA,
        "dangerous",
    ),
}

_TYPED_IN_SCOPE = (
    "vmware.host.datastores",
    "vmware.host.network_uplinks",
    "vmware.host.storage_devices",
    "vmware.host.usage",
    "vmware.host.vsan_health",
    "vmware.object.collect",
    "vmware.tasks.recent",
    "vmware.vm.info",
)
_TYPED = {op.op_id: op for op in VMWARE_TYPED_OPS if op.op_id in _TYPED_IN_SCOPE}

_PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    **{f"vmware.composite.{k}": v[0] for k, v in _COMPOSITES.items()},
    **{op_id: op.parameter_schema for op_id, op in _TYPED.items()},
}
_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    **{f"vmware.composite.{k}": v[1] for k, v in _COMPOSITES.items()},
    **{op_id: op.response_schema or {} for op_id, op in _TYPED.items()},
}
_GUIDANCE: dict[str, tuple[dict[str, Any], str]] = {
    **{
        f"vmware.composite.{k}": (GUEST_EVENT_PERF_LLM_INSTRUCTIONS[f"vmware.composite.{k}"], v[2])
        for k, v in _COMPOSITES.items()
    },
    **{op_id: (op.llm_instructions or {}, op.safety_level) for op_id, op in _TYPED.items()},
}


def test_every_typed_read_in_scope_is_registered() -> None:
    assert set(_TYPED) == set(_TYPED_IN_SCOPE)


@pytest.mark.parametrize("op_id", sorted(_PARAMETER_SCHEMAS))
def test_every_example_validates(op_id: str) -> None:
    schema = _PARAMETER_SCHEMAS[op_id]
    Draft202012Validator.check_schema(schema)
    examples = schema.get("examples")
    assert isinstance(examples, list) and examples, f"{op_id} has no example"
    for example in examples:
        assert validate_params(schema, example) == [], (op_id, example)


@pytest.mark.parametrize("op_id", sorted(_GUIDANCE))
def test_llm_instructions_have_the_keys_the_ai_reads(op_id: str) -> None:
    guidance, safety_level = _GUIDANCE[op_id]
    keys = {"when_to_call", "output_shape", "next_step"}
    if safety_level != "safe":
        keys.add("preconditions")
    assert keys <= set(guidance), (op_id, keys - set(guidance))
    assert "when_to_use" not in guidance
    assert "result_shape" not in guidance


def test_llm_instructions_cover_only_ops_in_this_table() -> None:
    expected = {f"vmware.composite.{s}" for s in _COMPOSITES}
    assert set(GUEST_EVENT_PERF_LLM_INSTRUCTIONS) == expected


def _lists_without_item_fields(schema: Any, path: str) -> list[str]:
    """Return the path of every array whose object items name no field."""
    if not isinstance(schema, dict):
        return []
    found: list[str] = []
    items = schema.get("items")
    if isinstance(items, dict):
        kinds = items.get("type")
        is_object = kinds == "object" or (isinstance(kinds, list) and "object" in kinds)
        if is_object and not items.get("properties"):
            found.append(path)
        found.extend(_lists_without_item_fields(items, f"{path}[]"))
    for name, sub in (schema.get("properties") or {}).items():
        found.extend(_lists_without_item_fields(sub, f"{path}.{name}"))
    return found


@pytest.mark.parametrize("op_id", sorted(_RESPONSE_SCHEMAS))
def test_response_lists_name_their_fields(op_id: str) -> None:
    schema = _RESPONSE_SCHEMAS[op_id]
    assert schema.get("properties"), op_id
    assert _lists_without_item_fields(schema, "") == []


def test_guest_file_read_keeps_its_scalars_on_a_large_file() -> None:
    guidance = GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.file.read"]
    assert guidance["result_scalars"]["keys"] == [
        "vm",
        "file_manager_moid",
        "guest_path",
        "size_bytes",
        "content_fetch",
        "content_encoding",
    ]
