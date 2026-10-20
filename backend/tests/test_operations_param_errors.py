# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the safe ``validation_errors`` entries (#3669)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from meho_backplane.operations._validate import validate_params

_SECRET = "eyJtoken" + "$value!123" + "abcdef"
_BAD = "Bearer " + _SECRET


@pytest.mark.parametrize(
    ("prop_schema", "value", "validator", "expected"),
    [
        ({"type": "integer"}, _BAD, "type", "type: integer"),
        ({"type": ["integer", "null"]}, _BAD, "type", "type: integer or null"),
        ({"enum": ["a", "b"]}, _BAD, "enum", 'one of: "a", "b"'),
        ({"const": "on"}, _BAD, "const", 'exactly: "on"'),
        ({"pattern": "^x$"}, _BAD, "pattern", 'string matching pattern "^x$"'),
        ({"maxLength": 3}, _BAD, "maxLength", "string with at most this many characters: 3"),
        ({"minLength": 99}, _BAD, "minLength", "string with at least this many characters: 99"),
        ({"minimum": 1}, 0, "minimum", "number >= 1"),
        ({"exclusiveMaximum": 5}, 5, "exclusiveMaximum", "number < 5"),
        ({"maxItems": 1}, [_BAD, _BAD], "maxItems", "list with at most this many items: 1"),
        ({"uniqueItems": True}, [_BAD, _BAD], "uniqueItems", "list without duplicate items"),
        (
            {"oneOf": [{"type": "integer"}, {"type": "boolean"}]},
            _BAD,
            "oneOf",
            "value matching exactly one of the allowed shapes",
        ),
    ],
)
def test_entry_names_expected_shape_without_the_value(
    prop_schema: dict[str, Any], value: Any, validator: str, expected: str
) -> None:
    schema = {"type": "object", "properties": {"field": prop_schema}}
    errors = validate_params(schema, {"field": value})
    assert len(errors) == 1
    entry = errors[0]
    assert entry["path"] == "$.field"
    assert entry["validator"] == validator
    assert entry["expected"] == expected
    assert entry["remediation"]
    assert _SECRET not in json.dumps(errors)


def test_enum_listing_is_bounded() -> None:
    schema = {"type": "object", "properties": {"f": {"enum": list(range(25))}}}
    (entry,) = validate_params(schema, {"f": "nope"})
    assert entry["expected"] == "one of: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9 (+15 more)"


def test_required_names_only_the_missing_property() -> None:
    schema = {"type": "object", "required": ["path", "mount"]}
    (entry,) = validate_params(schema, {"path": _BAD})
    assert entry == {
        "path": "$",
        "validator": "required",
        "expected": 'required property missing: "mount"',
        "remediation": "add the missing property to params",
    }


def test_additional_properties_names_key_and_declared_params() -> None:
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
    }
    (entry,) = validate_params(schema, {"path": "a", "pth": _BAD})
    assert entry["expected"] == """undeclared property: "pth" (not in the op's declared params)"""
    assert entry["remediation"] == 'remove the undeclared property; declared params are: "path"'
    assert _SECRET not in json.dumps(entry)


def test_undeclared_key_that_looks_like_a_secret_is_redacted() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    (entry,) = validate_params(schema, {_BAD: 1})
    assert _SECRET not in json.dumps(entry)
    assert "[REDACTED:" in entry["expected"]


def test_dependent_required_names_the_missing_partner() -> None:
    schema = {"type": "object", "dependentRequired": {"user": ["password"]}}
    (entry,) = validate_params(schema, {"user": "u"})
    assert entry["expected"] == 'properties required together: user needs "password"'


def test_unknown_keyword_falls_back_to_schema_hint() -> None:
    schema = {"type": "object", "properties": {"f": {"contains": {"type": "integer"}}}}
    (entry,) = validate_params(schema, {"f": [_BAD]})
    assert entry["validator"] == "contains"
    assert entry["expected"] == "value that satisfies the schema keyword 'contains'"
    assert "search_operations" in entry["remediation"]
    assert _SECRET not in json.dumps(entry)
