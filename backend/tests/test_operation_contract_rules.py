# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of the operation contract checker, with small fake operations.

The live check is in :mod:`tests.test_operation_contracts`. These tests prove
each rule fails with a message that names the problem, without a database.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from tests.test_operation_contracts import (
    DESCRIPTION_MAX,
    GAPS_FILE,
    READS_WITH_A_WRITE_WORD,
    SUMMARY_MAX,
    OperationContract,
    check_operation,
    compare_with_gaps,
    find_problems,
)

_GOOD_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM id."},
        "spec": {
            "type": "object",
            "description": "New settings.",
            "properties": {"cpu": {"type": "integer", "description": "CPU count."}},
        },
    },
    "required": ["vm"],
    "examples": [{"vm": "vm-1", "spec": {"cpu": 2}}],
}


def _good_op(**overrides: Any) -> OperationContract:
    values: dict[str, Any] = {
        "connector": "vmware-rest",
        "op_id": "fake.vm.update",
        "summary": "Change a VM's CPU count.",
        "description": "Sets the number of virtual CPUs of one VM.",
        "parameter_schema": _GOOD_PARAMS,
        "response_schema": {"type": "object", "properties": {"vm": {"type": "string"}}},
        "llm_instructions": {
            "when_to_call": "To change CPUs.",
            "output_shape": "The VM id.",
            "next_step": "Read the VM back.",
            "preconditions": "The VM is powered off.",
        },
        "safety_level": "caution",
        "requires_approval": True,
        "group_text": "VM changes.",
        "has_preview_builder": False,
    }
    values.update(overrides)
    return OperationContract(**values)


def _only_rule(op: OperationContract, rule: str) -> list[str]:
    result = check_operation(op)
    assert set(result) == {rule}, f"expected only rule {rule!r} to fail, got {result}"
    return result[rule]


def test_a_good_operation_passes_every_rule() -> None:
    assert check_operation(_good_op()) == {}


def test_a_list_response_with_item_fields_passes() -> None:
    op = _good_op(
        response_schema={"type": "array", "items": {"properties": {"vm": {"type": "string"}}}}
    )
    assert check_operation(op) == {}


def test_empty_description_fails() -> None:
    assert _only_rule(_good_op(description=""), "summary") == ["description is empty"]


def test_long_or_multi_line_summary_fails() -> None:
    problems = _only_rule(_good_op(summary="a\nb" + "x" * SUMMARY_MAX), "summary")
    assert "summary is more than one line" in problems
    assert any("characters (at most 120)" in p for p in problems)


def test_long_description_fails() -> None:
    problems = _only_rule(_good_op(description="x" * (DESCRIPTION_MAX + 1)), "summary")
    assert "llm_instructions" in problems[0]


def test_nested_field_without_description_fails() -> None:
    params = {
        **_GOOD_PARAMS,
        "properties": {
            **_GOOD_PARAMS["properties"],
            "spec": {
                "type": "object",
                "description": "New settings.",
                "properties": {"cpu": {"type": "integer"}},
            },
        },
    }
    problems = _only_rule(_good_op(parameter_schema=params), "field_descriptions")
    assert problems == ["parameter field 'spec.cpu' has no description"]


def test_field_inside_an_array_item_is_checked() -> None:
    params = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "description": "Tags.",
                "items": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        },
        "examples": [{"tags": [{"name": "a"}]}],
    }
    problems = _only_rule(_good_op(parameter_schema=params), "field_descriptions")
    assert problems == ["parameter field 'tags[].name' has no description"]


@pytest.mark.parametrize(
    "response_schema",
    [None, {}, {"type": "object"}, {"type": "array", "items": {"type": "object"}}],
)
def test_opaque_response_schema_fails(response_schema: dict[str, Any] | None) -> None:
    _only_rule(_good_op(response_schema=response_schema), "response_schema")


def test_missing_guidance_key_fails() -> None:
    op = _good_op(llm_instructions={"when_to_use": "x", "output_shape": "y", "next_step": "z"})
    problems = _only_rule(op, "llm_instructions")
    assert problems == [
        "llm_instructions has no 'when_to_call'",
        "llm_instructions has no 'preconditions'",
    ]


def test_a_read_needs_no_preconditions() -> None:
    guidance = {"when_to_call": "x", "output_shape": "y", "next_step": "z"}
    op = _good_op(
        op_id="fake.vm.get", safety_level="safe", requires_approval=False, llm_instructions=guidance
    )
    assert check_operation(op) == {}


def test_missing_example_fails() -> None:
    params = {k: v for k, v in _GOOD_PARAMS.items() if k != "examples"}
    problems = _only_rule(_good_op(parameter_schema=params), "examples")
    assert problems == ["parameter schema has no entry under 'examples'"]


def test_example_that_does_not_validate_fails() -> None:
    params = {**_GOOD_PARAMS, "examples": [{"spec": {"cpu": "two"}}]}
    problems = _only_rule(_good_op(parameter_schema=params), "examples")
    assert len(problems) == 2
    assert all(p.startswith("example 0 does not validate") for p in problems)


_ID_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "pattern": r"^vm-[0-9]+$", "description": "VM id."},
        "disks": {
            "type": "array",
            "description": "Disk ids.",
            "items": {"type": "string", "pattern": r"^[0-9]+$", "description": "Disk id."},
        },
    },
    "required": ["vm"],
}


def test_a_placeholder_may_stand_in_a_pattern_field() -> None:
    params = {**_ID_PARAMS, "examples": [{"vm": "<vm-id>", "disks": ["<disk-id>"]}]}
    assert check_operation(_good_op(parameter_schema=params)) == {}


def test_a_real_looking_value_must_still_match_the_pattern() -> None:
    params = {**_ID_PARAMS, "examples": [{"vm": "app-vm", "disks": ["<disk-id>"]}]}
    problems = _only_rule(_good_op(parameter_schema=params), "examples")
    assert problems == [
        'example 0 does not validate: $.vm expects string matching pattern "^vm-[0-9]+$"'
    ]


def test_a_placeholder_passes_only_the_pattern_check() -> None:
    params = {**_ID_PARAMS, "examples": [{"vm": "<vm-id>", "disks": "<disk-id>"}]}
    problems = _only_rule(_good_op(parameter_schema=params), "examples")
    assert problems == ["example 0 does not validate: $.disks expects type: array"]


def test_ticket_number_in_guidance_fails() -> None:
    guidance = {
        "when_to_call": "Added by #1234.",
        "output_shape": "y",
        "next_step": "z",
        "preconditions": "p",
    }
    problems = _only_rule(_good_op(llm_instructions=guidance), "no_ticket_numbers")
    assert problems == ["text names ticket #1234"]


def test_change_without_approval_fails() -> None:
    problems = _only_rule(_good_op(requires_approval=False), "requires_approval")
    assert problems == ["a 'caution' operation must have requires_approval: true"]


@pytest.mark.parametrize(("group_text", "message"), [(None, "no group"), ("  ", "no text")])
def test_missing_group_or_group_text_fails(group_text: str | None, message: str) -> None:
    problems = _only_rule(_good_op(group_text=group_text), "group")
    assert message in problems[0]


def test_destructive_without_preview_builder_fails() -> None:
    problems = _only_rule(_good_op(safety_level="destructive"), "preview_builder")
    assert "no preview builder" in problems[0]
    assert check_operation(_good_op(safety_level="destructive", has_preview_builder=True)) == {}


def test_safe_operation_with_a_write_word_fails() -> None:
    problems = _only_rule(
        _good_op(op_id="fake.vm.power.get", safety_level="safe", requires_approval=False),
        "write_name",
    )
    assert "'power'" in problems[0]
    assert "READS_WITH_A_WRITE_WORD" in problems[0]


def _safe_op(op_id: str) -> OperationContract:
    return _good_op(op_id=op_id, safety_level="safe", requires_approval=False)


@pytest.mark.parametrize("op_id", ["fake.vm.settings.get", "fake.node.runtime.show"])
def test_a_write_word_inside_a_longer_word_does_not_count(op_id: str) -> None:
    assert check_operation(_safe_op(op_id)) == {}


def test_an_allow_listed_read_passes_the_name_rule() -> None:
    assert check_operation(_safe_op(next(iter(READS_WITH_A_WRITE_WORD)))) == {}


def test_a_change_may_carry_a_write_word() -> None:
    assert check_operation(_good_op(op_id="fake.vm.power.start")) == {}


def test_a_new_gap_and_a_gone_gap_each_name_the_operation_and_rule() -> None:
    problems = {("vmware-rest", "fake.vm.update", "examples"): ["no example"]}
    known = {("nsx-rest", "fake.segment.list", "summary")}
    messages = compare_with_gaps(problems, known)
    assert messages == [
        "NEW GAP vmware-rest fake.vm.update rule 'examples': no example",
        "GONE nsx-rest fake.segment.list rule 'summary': the gap is fixed or the operation "
        f"no longer exists; remove this entry from {GAPS_FILE.name}",
    ]
    assert compare_with_gaps(problems, set(problems)) == []


_NESTED_FIELD_NO_DESCRIPTION: Final[dict[str, Any]] = {
    **_GOOD_PARAMS,
    "properties": {"vm": {"type": "object", "description": "VM.", "properties": {"id": {}}}},
    "examples": [{"vm": {"id": "vm-1"}}],
}


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"description": ""}, "summary"),
        ({"parameter_schema": _NESTED_FIELD_NO_DESCRIPTION}, "field_descriptions"),
        ({"response_schema": {"type": "object"}}, "response_schema"),
        ({"parameter_schema": {**_GOOD_PARAMS, "examples": []}}, "examples"),
        ({"parameter_schema": {**_GOOD_PARAMS, "examples": [{"vm": 1}]}}, "examples"),
        ({"requires_approval": False}, "requires_approval"),
        ({"safety_level": "destructive"}, "preview_builder"),
        ({"safety_level": "safe", "requires_approval": False}, "write_name"),
    ],
)
def test_each_bad_operation_fails_with_a_message_naming_operation_and_rule(
    overrides: dict[str, Any], rule: str
) -> None:
    messages = compare_with_gaps(find_problems([_good_op(**overrides)]), set())
    assert len(messages) == 1
    assert messages[0].startswith(f"NEW GAP vmware-rest fake.vm.update rule {rule!r}: ")
