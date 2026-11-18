# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The check at registration of the ``x-meho-switch`` keyword.

An operation registers only when MEHO can apply every keyword of its
parameter schema (:mod:`~meho_backplane.operations.switch_params`): the
keyword names a known switch, and it sits on a property. A typo or a
keyword in another place stops the operation from registering, so it
never shows a parameter that should stay hidden.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.operations.switch_params import (
    _DATA_KEYWORDS,
    _EXAMPLE_KEYWORDS,
    KNOWN_SWITCHES,
    SWITCH_MARKER,
    _pointer,
    switch_names,
)

__all__ = ["check_switch_names"]


def check_switch_names(schema: object, *, op_id: str) -> None:
    """Raise :class:`ValueError` when *schema* has a keyword MEHO cannot apply.

    That is a switch name MEHO does not know, or a keyword that is not on a
    property (:func:`_misplaced_markers`).
    """
    unknown = sorted(switch_names(schema) - KNOWN_SWITCHES.keys())
    if unknown:
        raise ValueError(
            f"op {op_id!r} names the unknown switch {unknown[0]!r} in "
            f"{SWITCH_MARKER!r}; the known switches are {sorted(KNOWN_SWITCHES)}"
        )
    misplaced = _misplaced_markers(schema)
    if misplaced:
        raise ValueError(
            f"op {op_id!r} has {SWITCH_MARKER!r} at {misplaced[0]!r}; the keyword "
            f"belongs on a property schema (or on a shared schema that only "
            f"properties link to)"
        )


#: The places at the root that hold shared schemas.
_SHARED_CONTAINERS: Final[tuple[tuple[str, ...], ...]] = (
    ("$defs",),
    ("definitions",),
    ("components", "schemas"),
)
#: The root keys of :data:`_SHARED_CONTAINERS`; the walk reads them on their own.
_SHARED_ROOTS: Final[frozenset[str]] = frozenset(path[0] for path in _SHARED_CONTAINERS)


def _marked(root: object, node: object) -> bool:
    """Return whether *node*, or a schema it links to, carries the keyword."""
    seen: set[str] = set()
    while isinstance(node, dict):
        if SWITCH_MARKER in node:
            return True
        ref = node.get("$ref")
        if not isinstance(ref, str) or ref in seen:
            return False
        seen.add(ref)
        node = _pointer(root, ref)
    return False


#: Keywords whose value is data or an example, not a schema.
_NOT_SCHEMAS: Final[frozenset[str]] = _DATA_KEYWORDS | _EXAMPLE_KEYWORDS | {"examples"}


def _shared_schemas(schema: object) -> list[tuple[object, str, bool]]:
    """Return each shared schema at the root of *schema*, with its path."""
    found: list[tuple[object, str, bool]] = []
    for path in _SHARED_CONTAINERS:
        defs = _pointer(schema, "#/" + "/".join(path))
        if isinstance(defs, dict):
            found.extend((item, "/" + "/".join((*path, name)), True) for name, item in defs.items())
    return found


def _children(node: dict[str, Any], path: str, *, root: bool) -> list[tuple[object, str, bool]]:
    """Return the schemas in *node*, each with its path and whether it is a property."""
    found: list[tuple[object, str, bool]] = []
    for key, value in node.items():
        if key in _NOT_SCHEMAS or (root and key in _SHARED_ROOTS):
            continue
        if key == "properties" and isinstance(value, dict):
            found.extend((prop, f"{path}/{key}/{name}", True) for name, prop in value.items())
        else:
            found.append((value, f"{path}/{key}", False))
    return found


def _misplaced_markers(schema: object) -> list[str]:
    """Return the sorted paths of the keywords MEHO would not apply.

    MEHO applies the keyword on a property schema (a value in
    ``properties``), and on a shared schema (``$defs``, ``definitions``,
    ``components.schemas``) that a property links to with ``$ref``. A
    keyword anywhere else (on ``items``, ``additionalProperties``, a
    ``patternProperties`` schema, a branch or the root), or a link to a
    marked shared schema from such a place, is misplaced.
    """
    found: list[str] = []
    pending: list[tuple[object, str, bool]] = [(schema, "", False), *_shared_schemas(schema)]
    while pending:
        node, path, allowed = pending.pop()
        if isinstance(node, list):
            pending.extend((item, f"{path}/{index}", False) for index, item in enumerate(node))
        elif isinstance(node, dict):
            if not allowed and _marked(schema, node):
                found.append(path or "/")
            pending.extend(_children(node, path, root=node is schema))
    return sorted(found)
