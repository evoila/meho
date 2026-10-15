# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded inline view of a document record (#3633).

When a large result has no single paginable collection -- a detail object
with several lists, a dict of scalars, or a scalar root -- the reducer keeps
the whole payload under a handle as a **document record**. The caller then
reads parts of it with ``result_query`` and an RFC 6901 ``path``.

This module builds the small, fixed-size pieces the caller sees inline
instead of the document: a field outline (names and kinds, never values),
a JSON Schema sketch for the handle, the handle's markdown summary, and the
drill-in hint with an example ``path``. Every helper is pure and bounded by
the constants below, so the inline view never grows with the document.
"""

from __future__ import annotations

import json
from typing import Any, Final
from uuid import UUID

from meho_backplane.jsonflux.query.json_pointer import json_type

__all__ = [
    "DOCUMENT_RECORD",
    "document_drill_in_rationale",
    "document_outline",
    "document_schema",
    "document_summary_md",
    "example_path",
]

#: Value of the ``result_record`` summary key for a document handle.
DOCUMENT_RECORD: Final = "document"

#: Most top-level fields the outline names.
_OUTLINE_MAX_FIELDS: Final = 64

#: Byte budget for the outline (field names plus kind labels).
_OUTLINE_BYTE_BUDGET: Final = 2048

#: Longest field name the outline repeats. Longer names are counted, not shown.
_OUTLINE_MAX_NAME_CHARS: Final = 128


def _describe(value: Any) -> str:
    """A short kind label with a size, e.g. ``array (3 items)``."""
    if isinstance(value, dict):
        return f"object ({len(value)} keys)"
    if isinstance(value, list):
        return f"array ({len(value)} items)"
    if isinstance(value, str):
        return f"string ({len(value)} chars)"
    return json_type(value)


def document_outline(document: Any) -> dict[str, str]:
    """Return ``{field: kind label}`` for the first top-level fields of a dict.

    Bounded by :data:`_OUTLINE_MAX_FIELDS`, :data:`_OUTLINE_BYTE_BUDGET`,
    and :data:`_OUTLINE_MAX_NAME_CHARS`. A non-dict document has no fields
    and returns an empty outline.
    """
    if not isinstance(document, dict):
        return {}
    outline: dict[str, str] = {}
    used = 0
    for key, value in document.items():
        if len(outline) >= _OUTLINE_MAX_FIELDS:
            break
        if not isinstance(key, str) or len(key) > _OUTLINE_MAX_NAME_CHARS:
            continue
        label = _describe(value)
        cost = len(key.encode("utf-8")) + len(label) + 6  # quotes, colon, comma
        if used + cost > _OUTLINE_BYTE_BUDGET:
            break
        outline[key] = label
        used += cost
    return outline


def document_schema(document: Any, outline: dict[str, str]) -> dict[str, Any]:
    """A shallow JSON Schema for the handle: root kind plus outlined fields."""
    marker = {"x-result-record": DOCUMENT_RECORD}
    if not isinstance(document, dict):
        return {"type": json_type(document), **marker}
    properties = {key: {"type": json_type(document[key])} for key in outline}
    return {"type": "object", "properties": properties, **marker}


def document_summary_md(
    root_type: str, document_bytes: int, outline: dict[str, str], field_count: int
) -> str:
    """Markdown summary for the handle: what was kept and how to read it."""
    heading = (
        f"One {root_type} result ({document_bytes} bytes) kept as a document "
        "handle. Read parts of it with `result_query` and a `path`."
    )
    if not outline:
        return heading
    lines = [f"- `{key}`: {label}" for key, label in outline.items()]
    hidden = field_count - len(outline)
    if hidden > 0:
        lines.append(f"- ... and {hidden} more fields")
    return f"{heading}\n\n" + "\n".join(lines)


def example_path(outline: dict[str, str]) -> str:
    """A ready-to-use pointer: the first outlined field, or ``""`` (the whole document).

    The field name is escaped per RFC 6901 (``~`` → ``~0``, ``/`` → ``~1``).
    """
    key = next(iter(outline), None)
    if key is None:
        return ""
    return "/" + key.replace("~", "~0").replace("/", "~1")


def document_drill_in_rationale(handle_id: UUID, tool: str, rest_route: str, path: str) -> str:
    """Agent-facing prose for a retained document the read-back tool can serve."""
    return (
        f"The full result is kept as one document. Read parts of it over MCP via "
        f"the ``{tool}`` tool or over REST via ``{rest_route}``: call either with "
        f"``handle_id={handle_id}`` and ``query={json.dumps({'path': path})}``. ``path`` "
        "is an RFC 6901 JSON Pointer (``/key/0/name``; ``~1`` means ``/`` and "
        "``~0`` means ``~`` inside a key; an empty path is the whole document). "
        "An array value pages with ``offset`` / ``limit``; any other value comes "
        "back whole in ``value``. Relational query fields do not apply to a "
        "document handle."
    )
