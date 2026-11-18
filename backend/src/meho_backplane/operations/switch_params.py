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
import re
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
#: Keywords whose value is one schema that describes the same value.
_SINGLE_BRANCH_KEYWORDS: Final[tuple[str, ...]] = ("then", "else")


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


def _branches(node: dict[str, Any]) -> list[object]:
    """Return the schemas in *node* that describe the same value as *node*."""
    found: list[object] = []
    for keyword in _BRANCH_KEYWORDS:
        branches = node.get(keyword)
        if isinstance(branches, list):
            found.extend(branches)
    found.extend(node[keyword] for keyword in _SINGLE_BRANCH_KEYWORDS if keyword in node)
    dependent = node.get("dependentSchemas")
    if isinstance(dependent, dict):
        found.extend(dependent.values())
    return found


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
        pending.extend(_branches(node))
    return found


def _all_hidden_names(root: object, schema: object) -> set[str]:
    """Return the hidden names of *schema* and of every branch of the same value."""
    return set().union(*(_hidden_names(root, node) for node in _property_schemas(root, schema)))


def _pattern_matches(pattern: str, key: str) -> bool:
    try:
        return re.search(pattern, key) is not None
    except re.error:
        return False


def _value_schemas(nodes: list[dict[str, Any]], key: str) -> list[object]:
    """Return the schemas that describe the value of *key* in an object of *nodes*."""
    found: list[object] = []
    for node in nodes:
        properties = node.get("properties")
        if isinstance(properties, dict) and key in properties:
            found.append(properties[key])
            continue
        patterns = node.get("patternProperties")
        matched = [
            schema
            for pattern, schema in (patterns.items() if isinstance(patterns, dict) else ())
            if _pattern_matches(pattern, key)
        ]
        found.extend(matched)
        if not matched and isinstance(node.get("additionalProperties"), dict):
            found.append(node["additionalProperties"])
    return found


def _item_schemas(nodes: list[dict[str, Any]], index: int) -> list[object]:
    """Return the schemas that describe item *index* of a list of *nodes*."""
    found: list[object] = []
    for node in nodes:
        prefix = node.get("prefixItems")
        if isinstance(prefix, list) and index < len(prefix):
            found.append(prefix[index])
        elif isinstance(node.get("items"), dict):
            found.append(node["items"])
    return found


def _strip_value(
    root: object, schema: object, value: Any, hidden: frozenset[str] = frozenset()
) -> Any:
    """Return the example *value* without the keys of hidden parameters, at any depth.

    *hidden* holds names hidden by a schema around *schema* for the same value.
    """
    nodes = _property_schemas(root, schema)
    if isinstance(value, dict):
        hidden = hidden.union(*(_hidden_names(root, node) for node in nodes))
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in hidden:
                continue
            for child in _value_schemas(nodes, key):
                item = _strip_value(root, child, item)
            result[key] = item
        return result
    if isinstance(value, list):
        out = list(value)
        for index, item in enumerate(out):
            for item_schema in _item_schemas(nodes, index):
                item = _strip_value(root, item_schema, item)
            out[index] = item
        return out
    return value


def _shown_value(root: object, node: dict[str, Any], key: str, hidden: frozenset[str]) -> object:
    """Return the shown value of an example keyword; ``_KEEP`` for any other."""
    value = node[key]
    if key in _EXAMPLE_KEYWORDS:
        return _strip_value(root, node, value, hidden)
    if key == "examples" and isinstance(value, list):
        return [_strip_value(root, node, item, hidden) for item in value]
    return _KEEP


class _Walk:
    """One walk over a schema: drop each hidden property (*show*) or refuse it."""

    def __init__(self, root: object, *, show: bool) -> None:
        self.root = root
        self.show = show

    def node(
        self,
        node: object,
        inherited: frozenset[str] = frozenset(),
        refs: frozenset[str] = frozenset(),
    ) -> object:
        """Walk *node*. *inherited* holds the names hidden around it for the same value."""
        if isinstance(node, list):
            return [self.node(item, refs=refs) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if inherited and isinstance(ref, str) and ref not in refs:
            target = _pointer(self.root, ref)
            if isinstance(target, dict):
                return self._inline(node, ref, target, inherited, refs)
        hidden = frozenset(inherited | _all_hidden_names(self.root, node))
        result = {key: self._keyword(node, key, hidden, refs) for key in node}
        result = {key: value for key, value in result.items() if value is not _KEEP}
        if not self.show and hidden:
            properties = result.setdefault("properties", {})
            if isinstance(properties, dict):
                for name in hidden:
                    properties[name] = False
        return result

    def _inline(
        self,
        node: dict[str, Any],
        ref: str,
        target: dict[str, Any],
        inherited: frozenset[str],
        refs: frozenset[str],
    ) -> object:
        """Walk the schema that *ref* links to in place, so the hidden names apply to it."""
        linked = self.node(target, inherited, refs | {ref})
        rest = {key: value for key, value in node.items() if key != "$ref"}
        if not rest:
            return linked
        return {"allOf": [linked, self.node(rest, inherited, refs)]}

    def _keyword(
        self,
        node: dict[str, Any],
        key: str,
        hidden: frozenset[str],
        refs: frozenset[str],
    ) -> object:
        """Return the walked value of *key* in *node*; ``_KEEP`` drops the keyword."""
        value = node[key]
        if self.show:
            shown = _shown_value(self.root, node, key, hidden)
            if shown is not _KEEP:
                return shown
        if key in _DATA_KEYWORDS:
            return value
        if key == "properties" and isinstance(value, dict):
            return {
                name: (False if name in hidden else self.node(prop, refs=refs))
                for name, prop in value.items()
                if not (self.show and name in hidden)
            }
        if key == "required" and isinstance(value, list):
            return [name for name in value if name not in hidden]
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
                name: self.node(item, hidden, refs)
                for name, item in value.items()
                if name not in hidden
            }
        if key in _BRANCH_KEYWORDS and isinstance(value, list):
            return [self.node(item, hidden, refs) for item in value]
        if key in _SINGLE_BRANCH_KEYWORDS:
            return self.node(value, hidden, refs)
        return self.node(value, refs=refs)


def _transform(root: object, node: object, *, show: bool) -> object:
    """Walk *node*; drop each hidden property (*show*) or make it accept nothing."""
    return _Walk(root, show=show).node(node)


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

    The parameter is also left out of ``required`` and ``dependentRequired``
    (also in the branches of the same object), and of ``example``,
    ``examples`` and ``default`` values at any depth, map values and list
    items included. Nothing else changes.
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
    accepts no value, so a call that gives it is refused. The object that
    has it, and each branch of that object (``allOf``, ``anyOf``,
    ``oneOf``, ``then``, ``else``), refuses it, and no ``required`` or
    ``dependentRequired`` list of them names it. Every other input is
    accepted or refused as before.
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
