# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A parameter that waits behind a switch that is off by default.

A parameter schema can carry the keyword ``x-meho-switch: "<switch name>"``.
The keyword counts at any depth, like ``x-meho-caller-address``. While the
named switch is off, MEHO treats the parameter as if the operation did not
have it:

* every place that shows the parameter schema leaves it out (the answer of
  ``search_operations``, the operation description, ``preview_operation``,
  the console and the CLI);
* a call that gives the parameter is refused like any unknown parameter,
  before it can wait for approval.

While the switch is on, the parameter shows and is accepted as usual.

The known switch names are in :data:`KNOWN_SWITCHES`: each maps to the
setting that holds the switch. A keyword with another name stops the
operation from registering (:func:`check_switch_names`), so a typo never
shows a parameter.

MEHO applies the rule where the operation row is read for the caller
(:func:`~meho_backplane.operations.op_policy.apply_effective_policy` and
the other readers of a stored schema), with :func:`visible_parameter_schema`.
The stored schema is never changed.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.settings import get_settings

__all__ = [
    "FILE_REF_PARAMS_SWITCH",
    "KNOWN_SWITCHES",
    "SWITCH_MARKER",
    "check_switch_names",
    "switch_names",
    "visible_parameter_schema",
]

SWITCH_MARKER: Final[str] = "x-meho-switch"
"""The schema keyword that keeps a parameter hidden while its switch is off."""

FILE_REF_PARAMS_SWITCH: Final[str] = "file_ref_params"
"""The switch for the file parameters (setting ``file_ref_params``)."""

KNOWN_SWITCHES: Final[Mapping[str, str]] = {FILE_REF_PARAMS_SWITCH: "file_ref_params"}
"""The known switch names, each with the name of the setting that holds it."""


def switch_names(schema: object) -> set[str]:
    """Return every switch name that *schema* names, at any depth."""
    found: set[str] = set()
    if isinstance(schema, dict):
        name = schema.get(SWITCH_MARKER)
        if name is not None:
            found.add(name if isinstance(name, str) else repr(name))
        for value in schema.values():
            found |= switch_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= switch_names(item)
    return found


def check_switch_names(schema: object, *, op_id: str) -> None:
    """Raise :class:`ValueError` when *schema* names a switch MEHO does not know."""
    unknown = sorted(switch_names(schema) - KNOWN_SWITCHES.keys())
    if unknown:
        raise ValueError(
            f"op {op_id!r} names the unknown switch {unknown[0]!r} in "
            f"{SWITCH_MARKER!r}; the known switches are {sorted(KNOWN_SWITCHES)}"
        )


def _switch_is_on(name: str) -> bool:
    setting = KNOWN_SWITCHES.get(name)
    return setting is not None and bool(getattr(get_settings(), setting, False))


def _hidden(property_schema: object) -> bool:
    if not isinstance(property_schema, dict):
        return False
    name = property_schema.get(SWITCH_MARKER)
    return isinstance(name, str) and not _switch_is_on(name)


def _strip(schema: object) -> tuple[object, bool]:
    """Return *schema* without the hidden parameters, and whether it changed."""
    if isinstance(schema, list):
        items = [_strip(item) for item in schema]
        if not any(changed for _, changed in items):
            return schema, False
        return [value for value, _ in items], True
    if not isinstance(schema, dict):
        return schema, False
    result: dict[str, Any] = {}
    changed = False
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            kept = {name: prop for name, prop in value.items() if not _hidden(prop)}
            changed = changed or len(kept) != len(value)
            value = kept
        new_value, inner = _strip(value)
        result[key] = new_value
        changed = changed or inner
    if not changed:
        return schema, False
    _drop_required_and_close(schema, result)
    return result, True


def _drop_required_and_close(original: dict[str, Any], result: dict[str, Any]) -> None:
    """Make *result* refuse a hidden name: no ``required`` entry, no extra names."""
    properties = original.get("properties")
    if not isinstance(properties, dict):
        return
    hidden = {name for name, prop in properties.items() if _hidden(prop)}
    if not hidden:
        return
    required = result.get("required")
    if isinstance(required, list):
        result["required"] = [name for name in required if name not in hidden]
    if "additionalProperties" not in result:
        result["additionalProperties"] = False


def visible_parameter_schema(schema: Any) -> Any:
    """Return *schema* without the parameters whose switch is off.

    Returns *schema* itself when nothing is hidden, so a schema without the
    keyword costs one walk and no copy. The result never shares a changed
    part with *schema*.
    """
    if not switch_names(schema):
        return schema
    stripped, changed = _strip(copy.deepcopy(schema))
    return stripped if changed else schema
