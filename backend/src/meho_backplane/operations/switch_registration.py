# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The check at registration of the ``x-meho-switch`` keyword.

An operation registers only when MEHO can apply every keyword of its
parameter schema (:mod:`~meho_backplane.operations.switch_params`): the
keyword names a known switch, it sits on a property, it is not under
``if``, ``then``, ``else`` or ``not``, and the schema links only with
plain ``#/`` pointers. A typo or a keyword in another place stops the
operation from registering, so it never shows a parameter that should
stay hidden.
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

    That is a switch name MEHO does not know, a keyword that is not on a
    property (:func:`_misplaced_markers`), a keyword under a condition
    (:func:`_conditional_markers`), or a keyword in a schema that links
    in a way MEHO does not follow (:func:`_unsupported_links`).
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
    conditional = _conditional_markers(schema)
    if conditional:
        raise ValueError(
            f"op {op_id!r} has {SWITCH_MARKER!r} under {conditional[0]!r}; the keyword "
            f"cannot be under 'if', 'then', 'else' or 'not'"
        )
    if switch_names(schema):
        links = _unsupported_links(schema)
        if links:
            raise ValueError(
                f"op {op_id!r} uses {SWITCH_MARKER!r} with the link {links[0]!r}; a "
                f"schema with this keyword may link only with plain '#/' pointers"
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
#: Keywords whose value maps a name to a schema. The walk reads the names
#: as names, never as keywords, so a schema named ``default`` is still read.
_SCHEMA_MAPS: Final[frozenset[str]] = frozenset(
    {"$defs", "definitions", "patternProperties", "dependentSchemas", "dependencies"}
)


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
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            found.extend((item, f"{path}/{key}/{name}", False) for name, item in value.items())
        elif key == "components" and isinstance(value, dict):
            found.extend(
                (item, f"{path}/{key}/{part}/{name}", False)
                for part, items in value.items()
                if isinstance(items, dict)
                for name, item in items.items()
            )
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


#: Keywords whose schema is a condition, or is chosen by one.
_CONDITIONS: Final[frozenset[str]] = frozenset({"if", "then", "else", "not"})
#: Link keywords that MEHO does not follow.
_LINK_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"}
)


def _schemas(schema: object) -> list[tuple[dict[str, Any], str]]:
    """Return every schema in *schema*, with its path, the shared ones too."""
    found: list[tuple[dict[str, Any], str]] = []
    pending: list[tuple[object, str]] = [(schema, "")]
    while pending:
        node, path = pending.pop()
        if isinstance(node, list):
            pending.extend((item, f"{path}/{index}") for index, item in enumerate(node))
        elif isinstance(node, dict):
            found.append((node, path or "/"))
            pending.extend((child, at) for child, at, _ in _children(node, path, root=False))
    return found


def _reaches_marker(root: object, node: object) -> bool:
    """Return whether *node*, or a schema in it or linked from it, carries the keyword."""
    seen: set[str] = set()
    pending: list[object] = [node]
    while pending:
        for item, _ in _schemas(pending.pop()):
            if SWITCH_MARKER in item:
                return True
            ref = item.get("$ref")
            if isinstance(ref, str) and ref not in seen:
                seen.add(ref)
                pending.append(_pointer(root, ref))
    return False


def _conditional_markers(schema: object) -> list[str]:
    """Return the sorted paths of the conditions that hold or link to the keyword."""
    return sorted(
        f"{path.rstrip('/')}/{key}"
        for node, path in _schemas(schema)
        for key in _CONDITIONS & node.keys()
        if _reaches_marker(schema, node[key])
    )


def _unsupported_links(schema: object) -> list[str]:
    """Return the sorted paths of the links in *schema* that MEHO does not follow.

    MEHO follows a ``$ref`` that is a plain ``#/`` pointer to a part of the
    same schema. Anchors, dynamic links, an ``$id`` below the root, and a
    ``$ref`` in any other form (another document, ``#name``, or a pointer
    with ``%`` escapes) are not followed.
    """
    found: list[str] = []
    for node, path in _schemas(schema):
        at = path.rstrip("/")
        found.extend(f"{at}/{key}" for key in sorted(_LINK_KEYWORDS & node.keys()))
        if "$id" in node and node is not schema:
            found.append(f"{at}/$id")
        ref = node.get("$ref")
        if ref is not None and not (
            isinstance(ref, str) and ref.startswith("#/") and "%" not in ref
        ):
            found.append(f"{at}/$ref")
    return sorted(found)
