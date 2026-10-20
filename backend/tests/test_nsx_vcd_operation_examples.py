# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every NSX and Cloud Director typed operation has an example that validates.

The AI copies an example call from the parameter schema, so each example must
pass the same schema MEHO checks before the call. Draft 2020-12, like the
dispatcher.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.nsx.typed_ops import NSX_TYPED_OPS
from meho_backplane.connectors.vcd.typed_ops import VCD_TYPED_OPS

_OPS: list[tuple[str, dict[str, Any]]] = [
    (op.op_id, op.parameter_schema) for op in (*NSX_TYPED_OPS, *VCD_TYPED_OPS)
]


@pytest.mark.parametrize(("op_id", "schema"), _OPS, ids=[op_id for op_id, _ in _OPS])
def test_each_example_validates_against_its_parameter_schema(
    op_id: str, schema: dict[str, Any]
) -> None:
    Draft202012Validator.check_schema(schema)
    examples = schema.get("examples")
    assert isinstance(examples, list) and examples, f"{op_id} has no example"
    validator = Draft202012Validator(schema)
    for example in examples:
        errors = [error.message for error in validator.iter_errors(example)]
        assert not errors, f"{op_id} example {example!r} does not validate: {errors}"
