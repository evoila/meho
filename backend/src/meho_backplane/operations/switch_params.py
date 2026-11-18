# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A parameter that waits behind a switch that is off by default.

A parameter schema can carry the keyword ``x-meho-switch: "<switch name>"``.
The keyword counts at any depth, like ``x-meho-caller-address``. A property
also counts as marked when it is a local ``$ref`` (for example
``#/$defs/...``) to a marked schema. While the named switch is off, MEHO
treats the parameter as if the operation did not have it:

* every place that shows the parameter schema leaves it out (the answer of
  ``search_operations``, the operation description, ``preview_operation``,
  the console and the CLI), also from ``required``, ``example``,
  ``examples`` and ``default`` (:func:`visible_parameter_schema`);
* a call that gives the parameter is refused before it can wait for
  approval (:func:`checked_parameter_schema`): the parameter gets a schema
  that accepts no value. All other inputs are accepted or refused exactly
  as before.

While the switch is on, the parameter shows and is accepted as usual.

The known switch names are in :data:`KNOWN_SWITCHES`: each maps to the
setting that holds the switch. A keyword with another name stops the
operation from registering (:func:`check_switch_names`), so a typo never
shows a parameter.

MEHO applies the rule where the operation row is read for the caller
(:func:`~meho_backplane.operations.op_policy.apply_effective_policy` and
the other readers of a stored schema). The stored schema is never changed.
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
    "checked_parameter_schema",
    "schema_for_checks",
    "switch_names",
    "visible_parameter_schema",
]

SWITCH_MARKER: Final[str] = "x-meho-switch"
"""The schema keyword that keeps a parameter hidden while its switch is off."""

FILE_REF_PARAMS_SWITCH: Final[str] = "file_ref_params"
"""The switch for the file parameters (setting ``file_ref_params``)."""

KNOWN_SWITCHES: Final[Mapping[str, str]] = {FILE_REF_PARAMS_SWITCH: "file_ref_params"}
"""The known switch names, each with the name of the setting that holds it."""

#: Keywords whose value is data, not a schema. The walk does not read a
#: ``properties`` key inside them as a schema keyword.
_DATA_KEYWORDS: Final[frozenset[str]] = frozenset({"enum", "const"})
#: Keywords whose value is an example of the input.
_EXAMPLE_KEYWORDS: Final[frozenset[str]] = frozenset({"example", "default"})
#: Marks "not a keyword that names inputs" in :func:`_shown_value`.
_KEEP: Final[object] = object()
#: Keywords whose value is a list of schemas that all describe the same value.
_BRANCH_KEYWORDS: Final[tuple[str, ...]] = ("allOf", "anyOf", "oneOf")


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


def _pointer(root: object, ref: str) -> object | None:
    """Return the part of *root* that the local link *ref* points to, or ``None``."""
    if not ref.startswith("#/"):
        return None
    target: object = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and key in target:
            target = target[key]
        elif isinstance(target, list) and key.isdigit() and int(key) < len(target):
            target = target[int(key)]
        else:
            return None
    return target


def _resolve(root: object, node: object) -> object:
    """Follow local ``$ref`` links from *node*; return the schema they end at."""
    seen: set[str] = set()
    while isinstance(node, dict):
        ref = node.get("$ref")
        if not isinstance(ref, str) or ref in seen:
            return node
        seen.add(ref)
        target = _pointer(root, ref)
        if target is None:
            return node
        node = target
    return node


def _hidden(root: object, property_schema: object) -> bool:
    """Return whether *property_schema*, or a schema it links to, is marked and off."""
    seen: set[str] = set()
    node = property_schema
    while isinstance(node, dict):
        name = node.get(SWITCH_MARKER)
        if isinstance(name, str):
            return not _switch_is_on(name)
        ref = node.get("$ref")
        if not isinstance(ref, str) or ref in seen:
            return False
        seen.add(ref)
        node = _pointer(root, ref)
    return False


def _hidden_names(root: object, schema: dict[str, Any]) -> set[str]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return set()
    return {name for name, prop in properties.items() if _hidden(root, prop)}


def _property_schemas(root: object, schema: object) -> list[dict[str, Any]]:
    """Return *schema* and the branches that describe the same object value."""
    found: list[dict[str, Any]] = []
    pending = [schema]
    seen: set[int] = set()
    while pending:
        node = _resolve(root, pending.pop())
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        found.append(node)
        for keyword in _BRANCH_KEYWORDS:
            branches = node.get(keyword)
            if isinstance(branches, list):
                pending.extend(branches)
    return found


def _strip_value(root: object, schema: object, value: Any) -> Any:
    """Return the example *value* without the keys of hidden parameters, at any depth."""
    nodes = _property_schemas(root, schema)
    if isinstance(value, dict):
        hidden: set[str] = set()
        children: dict[str, list[object]] = {}
        for node in nodes:
            hidden |= _hidden_names(root, node)
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, prop in properties.items():
                    children.setdefault(name, []).append(prop)
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in hidden:
                continue
            for child in children.get(key, []):
                item = _strip_value(root, child, item)
            result[key] = item
        return result
    if isinstance(value, list):
        item_schemas = [node["items"] for node in nodes if isinstance(node.get("items"), dict)]
        out = list(value)
        for item_schema in item_schemas:
            out = [_strip_value(root, item_schema, item) for item in out]
        return out
    return value


def _shown_value(root: object, node: dict[str, Any], key: str, hidden: set[str]) -> object:
    """Return the shown value of a keyword that names inputs; ``_KEEP`` for any other."""
    value = node[key]
    if key in _EXAMPLE_KEYWORDS:
        return _strip_value(root, node, value)
    if key == "examples" and isinstance(value, list):
        return [_strip_value(root, node, item) for item in value]
    if key == "dependentRequired" and isinstance(value, dict):
        return {
            name: [item for item in names if item not in hidden]
            if isinstance(names, list)
            else names
            for name, names in value.items()
            if name not in hidden
        }
    if key == "dependentSchemas" and isinstance(value, dict):
        return {
            name: _transform(root, item, show=True)
            for name, item in value.items()
            if name not in hidden
        }
    return _KEEP


def _transform(root: object, node: object, *, show: bool) -> object:
    """Walk *node*; drop each hidden property (*show*) or make it accept nothing."""
    if isinstance(node, list):
        return [_transform(root, item, show=show) for item in node]
    if not isinstance(node, dict):
        return node
    own = _hidden_names(root, node)
    hidden = own.union(*(_hidden_names(root, part) for part in _property_schemas(root, node)))
    result: dict[str, Any] = {}
    for key, value in node.items():
        shown = _shown_value(root, node, key, hidden) if show else _KEEP
        if shown is not _KEEP:
            result[key] = shown
        elif key in _DATA_KEYWORDS:
            result[key] = value
        elif key == "properties" and isinstance(value, dict):
            result[key] = {
                name: (False if name in own else _transform(root, prop, show=show))
                for name, prop in value.items()
                if not (show and name in own)
            }
        elif key == "required" and isinstance(value, list):
            result[key] = [name for name in value if name not in hidden]
        else:
            result[key] = _transform(root, value, show=show)
    return result


def _drop_unused_defs(stored: object, shown: object) -> object:
    """Drop the shared schemas that only hidden parameters used, or that are marked.

    A shared schema sits in ``$defs``, ``definitions`` or
    ``components.schemas`` at the root. One that no shown part links to is
    dropped when a link in *stored* used it or when it is marked and off.
    """
    if not isinstance(shown, dict):
        return shown
    used_before = _refs(stored)
    containers = [("$defs",), ("definitions",), ("components", "schemas")]
    changed = True
    while changed:
        changed = False
        used_now = _refs(shown)
        for path in containers:
            defs = _pointer(shown, "#/" + "/".join(path))
            if not isinstance(defs, dict):
                continue
            for name in list(defs):
                ref = "#/" + "/".join((*path, name.replace("~", "~0").replace("/", "~1")))
                if ref in used_now:
                    continue
                if ref in used_before or _hidden(shown, defs[name]):
                    del defs[name]
                    changed = True
    return shown


def _refs(node: object) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        for value in node.values():
            found |= _refs(value)
        return found
    if isinstance(node, list):
        return set().union(*(_refs(item) for item in node)) if node else set()
    return set()


def _off_switches(schema: object) -> bool:
    return any(not _switch_is_on(name) for name in switch_names(schema))


def visible_parameter_schema(schema: Any) -> Any:
    """Return *schema* as it is shown: without the parameters whose switch is off.

    The parameter is also left out of ``required``, of ``example``,
    ``examples`` and ``default`` values, at any depth. Nothing else changes.
    Returns *schema* itself when no switch it names is off. The result
    never shares a changed part with *schema*.
    """
    if not _off_switches(schema):
        return schema
    root = copy.deepcopy(schema)
    return _drop_unused_defs(root, _transform(root, root, show=True))


def checked_parameter_schema(schema: Any) -> Any:
    """Return *schema* as a call is checked against it while a switch is off.

    Each parameter whose switch is off gets the schema ``false``, which
    accepts no value, so a call that gives it is refused. It is dropped
    from ``required``. Every other input is accepted or refused as before.
    Returns *schema* itself when no switch it names is off.
    """
    if not _off_switches(schema):
        return schema
    root = copy.deepcopy(schema)
    return _transform(root, root, show=False)


def schema_for_checks(descriptor: object) -> Any:
    """Return the schema a call to *descriptor* is checked against.

    A row from :func:`~meho_backplane.operations.op_policy.apply_effective_policy`
    carries it in ``parameter_check_schema`` (its ``parameter_schema`` is the
    shown one). Any other row is checked against its stored schema.
    """
    checked = getattr(descriptor, "parameter_check_schema", None)
    if checked is not None:
        return checked
    return checked_parameter_schema(getattr(descriptor, "parameter_schema", None))
