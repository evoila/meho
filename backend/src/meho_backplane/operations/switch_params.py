# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A parameter that waits behind a switch that is off by default.

A parameter schema can carry the keyword ``x-meho-switch: "<switch name>"``.
The keyword goes on a property schema, at any depth. A property also counts
as marked when it is a local ``$ref`` (for example
``#/$defs/...``) to a marked schema. While the named switch is off, MEHO
treats the parameter as if the operation did not have it:

* every place that shows the parameter schema leaves it out (the answer of
  ``search_operations``, the operation description, ``preview_operation``,
  the console and the CLI), also from ``required``, ``example``,
  ``examples`` and ``default`` (:func:`visible_parameter_schema`);
* a call that gives the parameter is refused before it can wait for
  approval (:func:`checked_parameter_schema`), with the answer of an
  undeclared parameter. All other inputs are accepted or refused exactly
  as before;
* examples and hints in ``llm_instructions`` that name it are not shown
  (:func:`visible_llm_instructions`).

While the switch is on, the parameter shows and is accepted as usual.

The known switch names are in :data:`KNOWN_SWITCHES`: each maps to the
setting that holds the switch. A keyword with another name stops the
operation from registering, and so does a keyword that is not on a property
(:func:`~meho_backplane.operations.switch_registration.check_switch_names`).

MEHO applies the rule where the operation row is read for the caller
(:func:`~meho_backplane.operations.op_policy.apply_effective_policy` and
the other readers of a stored schema). The stored schema is never changed.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.settings import get_settings

__all__ = [
    "FILE_REF_PARAMS_SWITCH",
    "KNOWN_SWITCHES",
    "SWITCH_MARKER",
    "checked_parameter_schema",
    "hidden_parameter_names",
    "schema_for_checks",
    "switch_names",
    "visible_llm_instructions",
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
#: Branch keywords where the value must match one or more branches, not all.
_ALTERNATIVE_KEYWORDS: Final[tuple[str, ...]] = ("anyOf", "oneOf")


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
    """Return *schema* and the branches that describe the same object value.

    A schema with a local ``$ref`` counts with the schema it links to: both
    its own keywords and the linked ones describe the value.
    """
    found: list[dict[str, Any]] = []
    pending = [schema]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        found.append(node)
        pending.extend(_branches(node))
        ref = node.get("$ref")
        if isinstance(ref, str):
            pending.append(_pointer(root, ref))
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
        named = isinstance(properties, dict) and key in properties
        if isinstance(properties, dict) and named:
            found.append(properties[key])
        patterns = node.get("patternProperties")
        matched = [
            schema
            for pattern, schema in (patterns.items() if isinstance(patterns, dict) else ())
            if _pattern_matches(pattern, key)
        ]
        found.extend(matched)
        if not named and not matched and isinstance(node.get("additionalProperties"), dict):
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
        if isinstance(ref, str) and ref not in refs:
            target = _pointer(self.root, ref)
            if isinstance(target, dict) and (inherited or self._beside_ref(node)):
                return self._inline(node, ref, target, inherited, refs)
        hidden = frozenset(inherited | _all_hidden_names(self.root, node))
        result = {key: self._keyword(node, key, hidden, refs) for key in node}
        result = {key: value for key, value in result.items() if value is not _KEEP}
        if not self.show and hidden:
            _refuse(result, hidden)
        return result

    def _beside_ref(self, node: dict[str, Any]) -> set[str]:
        """Return the names hidden by the keywords beside the ``$ref`` of *node*.

        The schema that the link points to must not require them either, so
        the walk takes it in place with these names.
        """
        rest = {key: value for key, value in node.items() if key != "$ref"}
        return _all_hidden_names(self.root, rest)

    def _inline(
        self,
        node: dict[str, Any],
        ref: str,
        target: dict[str, Any],
        inherited: frozenset[str],
        refs: frozenset[str],
    ) -> object:
        """Walk the schema that *ref* links to in place, so the hidden names apply to it.

        The linked schema goes into the ``allOf`` of the same object, so
        keywords beside the link (such as ``unevaluatedProperties``) still
        see the properties it declares.
        """
        hidden = frozenset(inherited | _all_hidden_names(self.root, node))
        linked = self.node(target, hidden, refs | {ref})
        rest = {key: value for key, value in node.items() if key != "$ref"}
        if not rest:
            return linked
        walked = self.node(rest, hidden, refs)
        if not isinstance(walked, dict):
            return walked
        branches = walked.get("allOf")
        walked["allOf"] = [linked, *(branches if isinstance(branches, list) else [])]
        return walked

    def _branch_list(
        self, key: str, value: list[Any], hidden: frozenset[str], refs: frozenset[str]
    ) -> list[object]:
        """Walk the branches of *key*, each with the names hidden around it."""
        if key in _ALTERNATIVE_KEYWORDS:
            # A branch that requires a hidden name matches no call while the
            # switch is off, so it is left out. Without it the other branches
            # give the same answer as before (``oneOf`` still counts one).
            value = [item for item in value if not _requires(self.root, item, hidden)] or value
        return [self.node(item, hidden, refs) for item in value]

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
            return self._branch_list(key, value, hidden, refs)
        if key in _SINGLE_BRANCH_KEYWORDS:
            return self.node(value, hidden, refs)
        return self.node(value, refs=refs)


def _requires(root: object, schema: object, names: frozenset[str]) -> bool:
    """Return whether *schema* requires one of *names* (also through ``allOf`` and links)."""
    pending = [schema]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        required = node.get("required")
        if isinstance(required, list) and names.intersection(required):
            return True
        branches = node.get("allOf")
        pending.extend(branches if isinstance(branches, list) else [])
        ref = node.get("$ref")
        if isinstance(ref, str):
            pending.append(_pointer(root, ref))
    return False


def _refuse(result: dict[str, Any], hidden: frozenset[str]) -> None:
    """Make the object *result* refuse each name in *hidden*.

    An object that refuses undeclared names (``additionalProperties:
    false``) loses the name from ``properties``: a call that gives it gets
    the answer of an unknown name, and the error lists only shown names.
    Any other object (or a name that a pattern property matches) gets the
    schema ``false`` for it.
    """
    properties = result.setdefault("properties", {})
    if not isinstance(properties, dict):
        return
    patterns = result.get("patternProperties")
    pattern_list = list(patterns) if isinstance(patterns, dict) else []
    closed = result.get("additionalProperties") is False
    for name in hidden:
        if closed and not any(_pattern_matches(str(p), name) for p in pattern_list):
            properties.pop(name, None)
        else:
            properties[name] = False


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

    A call that gives a parameter whose switch is off is refused. The
    object that has it, and each branch of that object (``allOf``,
    ``anyOf``, ``oneOf``, ``then``, ``else``), refuses it: an object with
    ``additionalProperties: false`` leaves it out of ``properties``, any other gets
    the schema ``false`` for it. No ``required`` or ``dependentRequired``
    list of them names it. An ``anyOf`` or ``oneOf`` branch that requires
    it is left out, as it matches no call without it (unless every branch
    requires it). Every other input is accepted or refused as before.
    Returns *schema* itself when no switch it names is off.
    """
    if not _off_switches(schema):
        return schema
    root = copy.deepcopy(schema)
    return _transform(root, root, show=False)


def hidden_parameter_names(schema: object) -> set[str]:
    """Return the name of every property of *schema* that is hidden now, at any depth."""
    if not _off_switches(schema):
        return set()
    found: set[str] = set()
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            found |= _hidden_names(schema, node)
            pending.extend(
                value
                for key, value in node.items()
                if key not in _DATA_KEYWORDS and key not in _EXAMPLE_KEYWORDS and key != "examples"
            )
    return found


def visible_llm_instructions(llm_instructions: Any, schema: object) -> Any:
    """Return *llm_instructions* without what names a hidden parameter of *schema*.

    An example (in ``examples`` or ``example_call``) that names a hidden
    parameter is left out, and so is the ``parameter_hints`` entry of a
    hidden parameter. All other keys stay as they are. Returns
    *llm_instructions* itself when nothing is hidden.
    """
    names = hidden_parameter_names(schema)
    if not names or not isinstance(llm_instructions, Mapping):
        return llm_instructions
    words = re.compile(
        r"(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, sorted(names))) + r")(?![A-Za-z0-9_])"
    )

    def names_hidden(value: object) -> bool:
        return words.search(json.dumps(value, default=str)) is not None

    shown = dict(llm_instructions)
    listed = shown.get("examples")
    if isinstance(listed, list):
        shown["examples"] = [item for item in listed if not names_hidden(item)]
    if "example_call" in shown and names_hidden(shown["example_call"]):
        del shown["example_call"]
    hints = shown.get("parameter_hints")
    if isinstance(hints, Mapping):
        shown["parameter_hints"] = {name: hint for name, hint in hints.items() if name not in names}
    return shown


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
