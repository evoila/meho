# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build one safe, actionable entry per JSON Schema param violation (#3669).

:func:`describe_violation` turns a ``jsonschema`` ``ValidationError`` into
the dict that rides ``extras["validation_errors"]`` on an
``invalid_params`` result. Every field comes from the *schema* side of
the error (``validator``, ``validator_value``, ``schema``) -- never from
the caller's value (``err.instance``) and never from jsonschema's default
``err.message``, which quotes that value. A rejected value can be a
secret pasted into the wrong field, so echoing it back would put it in
cleartext into the response and the audit envelope.

The one instance-side fact used is the set of *key names* in an object
(to name an undeclared or missing property, or as a step of ``path``).
Key names are the caller's words for "which param", which is what they
need to fix the call. A key name can still look like a secret, so every
name runs through the credential redactor *raw*, before it is quoted or
escaped: JSON or JSON-path escaping turns a quote inside the name into
``\\"`` or ``\\'`` and a newline into ``\\n``, and the redactor's
patterns no longer match the escaped text. As defense in depth, every
built string then runs through the same redactor and length cap the
other error builders use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from jsonschema.exceptions import ValidationError

from meho_backplane.operations._errors import _sanitize_free_text

__all__ = ["describe_violation"]

#: Most enum values / property names listed in one ``expected`` string.
_MAX_LISTED: Final[int] = 10

#: Fallback hint: the full schema is one ``search_operations`` call away.
_SEE_SCHEMA: Final[str] = "call search_operations with this op_id to read the full parameter schema"


def _render(value: Any) -> str:
    """Render one schema-side value as compact JSON (strings keep quotes)."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _name(name: str) -> str:
    """Render one property name: redact the raw name first, then quote it."""
    return _render(_sanitize_free_text(name))


def _listing(rendered: list[str]) -> str:
    """Join up to :data:`_MAX_LISTED` already rendered items; count the rest."""
    shown = ", ".join(rendered[:_MAX_LISTED])
    rest = len(rendered) - _MAX_LISTED
    return f"{shown} (+{rest} more)" if rest > 0 else shown


def _names(names: list[str]) -> str:
    return _listing([_name(name) for name in names])


def _instance_keys(err: ValidationError) -> list[str]:
    instance = err.instance
    return [str(key) for key in instance] if isinstance(instance, Mapping) else []


#: A key that JSON path writes as ``.key``; any other key goes in ``['...']``.
_PLAIN_KEY: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _json_path(err: ValidationError) -> str:
    """Build the JSON path of the bad spot from redacted key names.

    Same shape as jsonschema's ``err.json_path`` (``$.a[0]['b c']``), but
    each caller-supplied key is redacted before the path escapes it.
    """
    path = "$"
    for step in err.absolute_path:
        if isinstance(step, int):
            path += f"[{step}]"
            continue
        key = _sanitize_free_text(str(step))
        if _PLAIN_KEY.match(key):
            path += f".{key}"
        else:
            path += "['" + key.replace("\\", "\\\\").replace("'", "\\'") + "']"
    return path


def _type(err: ValidationError) -> tuple[str, str]:
    value = err.validator_value
    types = value if isinstance(value, list) else [value]
    joined = " or ".join(str(t) for t in types)
    return f"type: {joined}", f"send a value of type {joined} at this path"


def _enum(err: ValidationError) -> tuple[str, str]:
    values = err.validator_value if isinstance(err.validator_value, list) else []
    return f"one of: {_listing([_render(v) for v in values])}", "use one of the listed values"


def _const(err: ValidationError) -> tuple[str, str]:
    return f"exactly: {_render(err.validator_value)}", "send exactly this value"


def _required(err: ValidationError) -> tuple[str, str]:
    required = err.validator_value if isinstance(err.validator_value, list) else []
    present = set(_instance_keys(err))
    missing = [str(name) for name in required if name not in present]
    return (
        f"required property missing: {_names(missing)}",
        "add the missing property to params",
    )


def _additional(err: ValidationError) -> tuple[str, str]:
    schema = err.schema if isinstance(err.schema, Mapping) else {}
    declared = [str(name) for name in schema.get("properties", {})]
    patterns = [str(p) for p in schema.get("patternProperties", {})]
    extra = [
        key
        for key in _instance_keys(err)
        if key not in declared and not any(re.search(p, key) for p in patterns)
    ]
    hint = "remove the undeclared property"
    if declared:
        hint += f"; declared params are: {_names(declared)}"
    return (
        f"undeclared property: {_names(extra)} (not in the op's declared params)",
        hint,
    )


def _dependent(err: ValidationError) -> tuple[str, str]:
    deps = err.validator_value if isinstance(err.validator_value, Mapping) else {}
    present = set(_instance_keys(err))
    pairs = [
        f"{name} needs {_names([str(d) for d in needs if d not in present])}"
        for name, needs in deps.items()
        if name in present and isinstance(needs, list) and any(d not in present for d in needs)
    ]
    return (
        f"properties required together: {'; '.join(pairs)}",
        "add the properties that must come together, or drop the one that needs them",
    )


def _bound(label: str, hint: str) -> Callable[[ValidationError], tuple[str, str]]:
    """Make a describer for a keyword whose value is one number or string."""

    def describe(err: ValidationError) -> tuple[str, str]:
        return f"{label} {_render(err.validator_value)}", hint

    return describe


def _pattern(err: ValidationError) -> tuple[str, str]:
    """Describe a pattern miss; add the field's own schema description to the hint.

    The description often says where the right value comes from (for an id
    field: the op that reads the id). It is schema text, not the caller's value.
    """
    hint = "change the value so it matches the pattern"
    schema = err.schema if isinstance(err.schema, Mapping) else {}
    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        hint += f". Field: {description.strip()}"
    return f"string matching pattern {_render(err.validator_value)}", hint


def _fixed(expected: str, hint: str) -> Callable[[ValidationError], tuple[str, str]]:
    def describe(err: ValidationError) -> tuple[str, str]:
        return expected, hint

    return describe


_DESCRIBERS: Final[dict[str, Callable[[ValidationError], tuple[str, str]]]] = {
    "type": _type,
    "enum": _enum,
    "const": _const,
    "required": _required,
    "additionalProperties": _additional,
    "unevaluatedProperties": _fixed(
        "no undeclared properties", "remove properties the op does not declare"
    ),
    "dependentRequired": _dependent,
    "pattern": _pattern,
    "format": _bound("string in format", "change the value to this format"),
    "minLength": _bound("string with at least this many characters:", "use a longer value"),
    "maxLength": _bound("string with at most this many characters:", "use a shorter value"),
    "minimum": _bound("number >=", "use a larger number"),
    "maximum": _bound("number <=", "use a smaller number"),
    "exclusiveMinimum": _bound("number >", "use a larger number"),
    "exclusiveMaximum": _bound("number <", "use a smaller number"),
    "multipleOf": _bound("number that is a multiple of", "use a multiple of this number"),
    "minItems": _bound("list with at least this many items:", "add items to the list"),
    "maxItems": _bound("list with at most this many items:", "remove items from the list"),
    "uniqueItems": _fixed("list without duplicate items", "remove the duplicate items"),
    "minProperties": _bound("object with at least this many properties:", "add properties"),
    "maxProperties": _bound("object with at most this many properties:", "remove properties"),
    "oneOf": _fixed("value matching exactly one of the allowed shapes", _SEE_SCHEMA),
    "anyOf": _fixed("value matching at least one of the allowed shapes", _SEE_SCHEMA),
    "not": _fixed("value that does not match the excluded shape", _SEE_SCHEMA),
}


def describe_violation(err: ValidationError) -> dict[str, Any]:
    """Return the safe ``validation_errors`` entry for one violation.

    Keys: ``path`` (JSON path of the bad spot), ``validator`` (the schema
    keyword that failed), ``expected`` (what the schema wants there) and
    ``remediation`` (a short, fixed hint). No field carries the rejected
    value. The path is built from redacted key names too: under
    ``additionalProperties`` or ``patternProperties`` it holds a
    caller-supplied key name.
    """
    validator = str(err.validator)
    describer = _DESCRIBERS.get(validator)
    if describer is None:
        expected = f"value that satisfies the schema keyword {validator!r}"
        remediation = _SEE_SCHEMA
    else:
        expected, remediation = describer(err)
    return {
        "path": _sanitize_free_text(_json_path(err)),
        "validator": validator,
        "expected": _sanitize_free_text(expected),
        "remediation": _sanitize_free_text(remediation),
    }
