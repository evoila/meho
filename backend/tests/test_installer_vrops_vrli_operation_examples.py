# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Contract tests for the VCF Installer, vROps and vRLI typed operations.

Every operation ships one example call, and the example is a valid call
(``jsonschema``, Draft 2020-12). Every response schema names its fields, and
the AI guidance has the keys the operation contract check asks for. The
operations are the 14 typed Installer operations, the Installer bring-up
multi-step operation, the 4 vROps reads (shared by the 8.x connector) and the
vRLI event query (shared by the 8.x connector).
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vcf_installer import bringup as installer_bringup
from meho_backplane.connectors.vcf_installer.typed_ops import INSTALLER_TYPED_OPS
from meho_backplane.connectors.vcf_logs.typed_ops import VRLI_TYPED_OPS
from meho_backplane.connectors.vcf_operations.typed_ops import VROPS_TYPED_OPS

_TYPED_OPS: list[Any] = [*INSTALLER_TYPED_OPS, *VROPS_TYPED_OPS, *VRLI_TYPED_OPS]


def _contract(op: Any) -> tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    return op.op_id, op.parameter_schema, op.response_schema, op.llm_instructions


_CONTRACTS = [_contract(op) for op in _TYPED_OPS] + [
    (
        installer_bringup.INSTALLER_BRINGUP_OP_ID,
        installer_bringup._BRINGUP_PARAM_SCHEMA,
        installer_bringup.BRINGUP_COMPOSITE_RESPONSE,
        installer_bringup._BRINGUP_LLM_INSTRUCTIONS,
    )
]
_IDS = [contract[0] for contract in _CONTRACTS]


def test_the_scope_has_twenty_operations() -> None:
    assert len(_CONTRACTS) == 20
    assert len(set(_IDS)) == 20


@pytest.mark.parametrize("contract", _CONTRACTS, ids=_IDS)
def test_each_example_is_a_valid_call(contract: tuple[Any, ...]) -> None:
    op_id, parameter_schema, _, _ = contract
    examples = parameter_schema.get("examples")
    assert isinstance(examples, list) and examples, op_id
    Draft202012Validator.check_schema(parameter_schema)
    validator = Draft202012Validator(parameter_schema)
    for example in examples:
        assert list(validator.iter_errors(example)) == [], (op_id, example)


@pytest.mark.parametrize("contract", _CONTRACTS, ids=_IDS)
def test_each_response_schema_names_its_fields(contract: tuple[Any, ...]) -> None:
    op_id, _, response_schema, _ = contract
    assert response_schema is not None, op_id
    assert response_schema.get("properties"), op_id


@pytest.mark.parametrize("contract", _CONTRACTS, ids=_IDS)
def test_each_op_has_the_guidance_keys(contract: tuple[Any, ...]) -> None:
    op_id, _, _, instructions = contract
    assert instructions is not None, op_id
    for key in ("when_to_call", "output_shape", "next_step"):
        value = instructions.get(key)
        assert isinstance(value, str) and value.strip(), (op_id, key)


@pytest.mark.parametrize(
    "op",
    [op for op in INSTALLER_TYPED_OPS if op.safety_level != "safe"],
    ids=lambda op: op.op_id,
)
def test_each_installer_change_has_preconditions(op: Any) -> None:
    assert op.llm_instructions is not None
    assert op.llm_instructions.get("preconditions", "").strip(), op.op_id


def test_bringup_composite_has_preconditions() -> None:
    assert installer_bringup._BRINGUP_LLM_INSTRUCTIONS["preconditions"].strip()


def test_resource_query_rejects_a_value_outside_the_vendor_enum() -> None:
    """The three resource filters take only the values the vendor lists."""
    op = next(op for op in VROPS_TYPED_OPS if op.op_id == "vrops.resource.query")
    validator = Draft202012Validator(op.parameter_schema)
    assert list(validator.iter_errors({"resourceHealth": ["RED", "GREY"]})) == []
    assert list(validator.iter_errors({"resourceHealth": ["PURPLE"]}))
    assert list(validator.iter_errors({"resourceState": ["RUNNING"]}))
    assert list(validator.iter_errors({"resourceStatus": ["RECEIVING"]}))


def test_resource_query_takes_one_name_and_not_name_with_regex() -> None:
    """The vendor supports one name, and uses name over regex when both are sent."""
    op = next(op for op in VROPS_TYPED_OPS if op.op_id == "vrops.resource.query")
    validator = Draft202012Validator(op.parameter_schema)
    assert list(validator.iter_errors({"name": ["<vm-name>"]})) == []
    assert list(validator.iter_errors({"regex": ["<vm-name>.*"]})) == []
    assert list(validator.iter_errors({"resourceKind": ["VirtualMachine"]})) == []
    assert list(validator.iter_errors({"name": ["<vm-name>", "<other-vm-name>"]}))
    assert list(validator.iter_errors({"name": ["<vm-name>"], "regex": ["<vm-name>.*"]}))
