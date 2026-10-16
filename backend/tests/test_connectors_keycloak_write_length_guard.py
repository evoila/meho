# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Length guard on Keycloak write params (#3628).

Keycloak keeps client and user text fields in 255-character database
columns. A longer value fails the database write, and Keycloak answers
with an HTTP 500 ``unknown_error`` that names no cause. The write schemas
carry ``maxLength: 255`` so the dispatcher's parameter check (the same
:func:`validate_params` call it runs before the policy gate) rejects the
call as ``invalid_params`` before any approval is requested.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.keycloak.ops_write_schemas import WRITE_OPS
from meho_backplane.operations._validate import validate_params

_SCHEMAS: dict[str, dict[str, Any]] = {op.op_id: op.parameter_schema for op in WRITE_OPS}

_BOUNDED_REPRESENTATION_FIELDS = [
    ("keycloak.client.create", "clientId"),
    ("keycloak.client.create", "name"),
    ("keycloak.client.create", "description"),
    ("keycloak.client.update", "clientId"),
    ("keycloak.client.update", "name"),
    ("keycloak.client.update", "description"),
    ("keycloak.user.create", "username"),
    ("keycloak.user.create", "email"),
    ("keycloak.user.create", "firstName"),
    ("keycloak.user.create", "lastName"),
]


@pytest.mark.parametrize(("op_id", "field"), _BOUNDED_REPRESENTATION_FIELDS)
def test_representation_field_over_255_is_rejected(op_id: str, field: str) -> None:
    errors = validate_params(_SCHEMAS[op_id], {"representation": {field: "x" * 256}})
    assert len(errors) == 1
    assert errors[0]["validator"] == "maxLength"
    assert errors[0]["path"] == f"$.representation.{field}"


@pytest.mark.parametrize(("op_id", "field"), _BOUNDED_REPRESENTATION_FIELDS)
def test_representation_field_at_255_is_accepted(op_id: str, field: str) -> None:
    assert validate_params(_SCHEMAS[op_id], {"representation": {field: "x" * 255}}) == []


@pytest.mark.parametrize(
    ("op_id", "param"),
    [("keycloak.client.create", "client_id"), ("keycloak.user.create", "username")],
)
def test_top_level_name_param_over_255_is_rejected(op_id: str, param: str) -> None:
    errors = validate_params(_SCHEMAS[op_id], {"representation": {}, param: "x" * 256})
    assert [e["validator"] for e in errors] == ["maxLength"]


def test_guard_counts_characters_not_bytes() -> None:
    """The database limit is 255 characters, so multi-byte text up to 255 passes."""
    schema = _SCHEMAS["keycloak.client.create"]
    assert validate_params(schema, {"representation": {"description": "é" * 255}}) == []


def test_guard_leaves_other_fields_and_types_alone() -> None:
    """Unbounded fields, null values, and extra keys still pass as before."""
    schema = _SCHEMAS["keycloak.client.create"]
    params = {
        "representation": {
            "clientId": "web",
            "description": None,
            "redirectUris": ["https://example.com/" + "a" * 400],
            "attributes": {"note": "y" * 1000},
        }
    }
    assert validate_params(schema, params) == []
