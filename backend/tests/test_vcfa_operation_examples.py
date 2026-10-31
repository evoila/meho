# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every VCF Automation 9 and Aria Automation 8 typed operation has a full contract.

The AI copies an example call from the parameter schema, so each example must
pass the same schema MEHO checks before the call (Draft 2020-12, like the
dispatcher). Each response schema must also name its main fields, nested ones
too, and no example may carry a value that looks real.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.vcf_automation.content_ops import VCFA_CONTENT_OPS
from meho_backplane.connectors.vcf_automation.deploy_ops import VCFA_DEPLOY_OPS
from meho_backplane.connectors.vcf_automation.feature_flag_ops import VCFA_FEATURE_FLAG_OPS
from meho_backplane.connectors.vcf_automation.image_ops import VCFA_IMAGE_OPS
from meho_backplane.connectors.vcf_automation.network_ops import VCFA_NETWORK_OPS
from meho_backplane.connectors.vcf_automation.provisioning_ops import VCFA_PROVISIONING_OPS
from meho_backplane.connectors.vcf_automation.typed_ops import VCFA_TYPED_OPS
from meho_backplane.connectors.vra8.content_ops import VRA8_CONTENT_OPS
from meho_backplane.connectors.vra8.typed_ops import VRA8_TYPED_OPS

_OPS: list[Any] = [
    *VCFA_TYPED_OPS,
    *VCFA_PROVISIONING_OPS,
    *VCFA_IMAGE_OPS,
    *VCFA_NETWORK_OPS,
    *VCFA_DEPLOY_OPS,
    *VCFA_FEATURE_FLAG_OPS,
    *VCFA_CONTENT_OPS,
    *VRA8_TYPED_OPS,
    *VRA8_CONTENT_OPS,
]

#: An IPv4 address or a host name with a dot and a real-looking top-level domain.
_REAL_LOOKING = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b|\b[a-z0-9-]+\.(com|net|org|local|lan)\b")


def test_all_61_operations_are_covered() -> None:
    assert len({op.op_id for op in _OPS}) == 61


@pytest.mark.parametrize("op", _OPS, ids=lambda op: op.op_id)
def test_each_example_validates_against_its_parameter_schema(op: Any) -> None:
    schema = op.parameter_schema
    Draft202012Validator.check_schema(schema)
    examples = schema.get("examples")
    assert isinstance(examples, list) and examples, f"{op.op_id} has no example"
    validator = Draft202012Validator(schema)
    for example in examples:
        errors = [error.message for error in validator.iter_errors(example)]
        assert not errors, f"{op.op_id} example {example!r} does not validate: {errors}"
        assert not _REAL_LOOKING.search(repr(example)), f"{op.op_id} example looks real"


def _properties(schema: Any) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every named property in *schema*, nested ones too."""
    if not isinstance(schema, Mapping):
        return
    for name, sub in (schema.get("properties") or {}).items():
        yield name, sub
        yield from _properties(sub)
    yield from _properties(schema.get("items"))


@pytest.mark.parametrize("op", _OPS, ids=lambda op: op.op_id)
def test_each_response_schema_names_and_describes_its_fields(op: Any) -> None:
    schema = op.response_schema
    assert isinstance(schema, Mapping) and schema.get("properties"), op.op_id
    Draft202012Validator.check_schema(schema)
    undescribed = [name for name, sub in _properties(schema) if not sub.get("description")]
    assert not undescribed, f"{op.op_id} response fields without a description: {undescribed}"
