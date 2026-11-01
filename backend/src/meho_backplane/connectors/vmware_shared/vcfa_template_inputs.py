# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which inputs of a cloud template are secret, and the check that they get no value.

``*.blueprint.validate`` takes ``inputs``: values for the inputs of the
template it checks. A secret input (a password, key or token) may only be
named by a reference such as ``${secret.<name>}``; a value would travel to
the vendor. Two rules say which inputs are secret:

* the name is a secret name
  (:func:`~meho_backplane.broadcast.events.is_secret_param_name`);
* the template marks the input ``encrypted: true``. For YAML given in
  ``content``, :func:`encrypted_inputs_schema` reads that mark, also from
  the ``properties`` of an object input and the ``items`` of a list input,
  at any depth. For a stored template, the handler reads the vendor's
  inputs schema.

:func:`refuse_secret_input_values` applies both rules to the values, also
inside an object input and in each item of a list input, and raises
before any vendor call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

import yaml

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.vmware_shared.vcfa_yaml_secrets import is_reference

__all__ = ["encrypted_inputs_schema", "refuse_secret_input_values"]

#: A YAML true as the parser reads it: bare or quoted (YAML 1.1 also reads
#: ``yes`` and ``on`` as true).
_TRUE_SCALAR: Final[re.Pattern[str]] = re.compile(r"(?i:true|yes|on)")


def refuse_secret_input_values(inputs: Mapping[str, Any], schema: Mapping[str, Any] | None) -> None:
    """Refuse ``inputs`` that give a value, not a reference, to a secret input.

    An input is secret when its name is a secret name, or when *schema*
    (the template's inputs schema, a JSON schema) marks it ``encrypted``.
    The values of an object input are checked the same way, against its
    ``properties``, and each item of a list input against its ``items``.
    Raises :exc:`ValueError` naming the inputs (``login.pin``, ``logins[0].pin``).
    """
    literal = _literal_secret_inputs(inputs, schema, secret=False)
    if literal:
        raise ValueError(
            f"the secret input(s) {', '.join(literal)} hold a value; MEHO does not send "
            "it. Give a reference such as ${secret.<name>}, or leave the input out"
        )


def _literal_secret_inputs(
    values: Mapping[str, Any], schema: Mapping[str, Any] | None, *, secret: bool
) -> list[str]:
    """The names (``a.b`` when nested) of the secret inputs in *values* with a value."""
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    found: list[str] = []
    for name, value in values.items():
        child = properties.get(name) if isinstance(properties, Mapping) else None
        child_secret = (
            secret
            or is_secret_param_name(str(name))
            or (isinstance(child, Mapping) and child.get("encrypted") is True)
        )
        if isinstance(value, Mapping):
            found.extend(
                f"{name}.{inner}"
                for inner in _literal_secret_inputs(value, child, secret=child_secret)
            )
        elif isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
            items = child.get("items") if isinstance(child, Mapping) else None
            found.extend(
                f"{name}[{position}].{inner}"
                for position, item in enumerate(value)
                for inner in _literal_secret_inputs(item, items, secret=child_secret)
            )
        elif child_secret and not is_reference(value):
            found.append(str(name))
    return sorted(found)


def encrypted_inputs_schema(content: str) -> dict[str, Any]:
    """The ``encrypted`` inputs of template YAML, in the shape of an inputs schema.

    The YAML is composed, not loaded: an alias stays a pointer, so a
    document built to blow up on load does not. YAML the parser refuses
    gives no secret inputs; the vendor reports the error.
    """
    try:
        root = yaml.compose(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return {}
    return _encrypted_schema(_mapping_value(root, "inputs"), set())


def _encrypted_schema(node: Any, seen: set[int]) -> dict[str, Any]:
    """The inputs schema of one map of input definitions.

    Each definition keeps its ``encrypted`` mark, its nested ``properties``
    (an object input) and its ``items`` (a list input), read the same way.
    *seen* holds the nodes on the way down: an alias that points back up
    is not followed twice.
    """
    properties: dict[str, Any] = {}
    if id(node) in seen:
        return {"properties": properties}
    seen = seen | {id(node)}
    for name, definition in _mapping_pairs(node):
        entry: dict[str, Any] = {}
        encrypted = _mapping_value(definition, "encrypted")
        if isinstance(encrypted, yaml.ScalarNode) and _TRUE_SCALAR.fullmatch(encrypted.value):
            entry["encrypted"] = True
        nested = _encrypted_schema(_mapping_value(definition, "properties"), seen)
        if nested["properties"]:
            entry["properties"] = nested["properties"]
        items = _encrypted_schema(
            _mapping_value(_mapping_value(definition, "items"), "properties"), seen
        )
        if items["properties"]:
            entry["items"] = {"properties": items["properties"]}
        if entry:
            properties[name] = entry
    return {"properties": properties}


def _mapping_pairs(node: Any) -> list[tuple[str, Any]]:
    """The ``(key, value node)`` pairs of a mapping node with plain keys."""
    if not isinstance(node, yaml.MappingNode):
        return []
    return [(key.value, value) for key, value in node.value if isinstance(key, yaml.ScalarNode)]


def _mapping_value(node: Any, key: str) -> Any:
    """The value node under *key* of a mapping node, or ``None``."""
    return next((value for name, value in _mapping_pairs(node) if name == key), None)
