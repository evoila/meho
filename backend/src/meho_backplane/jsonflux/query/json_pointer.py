# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RFC 6901 JSON Pointer parsing and resolution for retained documents.

The ``result_query`` path reader uses this module to read one value out of a
document record. It follows RFC 6901 (https://www.rfc-editor.org/rfc/rfc6901):

* The empty pointer ``""`` points at the whole document.
* Every other pointer starts with ``/``; each ``/``-separated token is one
  step down into the document.
* ``~1`` decodes to ``/`` and ``~0`` decodes to ``~``, in that order. Any
  other ``~`` sequence is a syntax error.
* An array step must be an unsigned base-10 index with no leading zeros.
  ``-`` names the element after the last one, which never exists, so it does
  not resolve.

Syntax is checked without a document (:func:`parse_json_pointer`); array
index rules and missing members can only be checked against the document
(:func:`resolve_json_pointer`). Both raise :class:`JsonPointerError`, a
``ValueError``, so a pydantic validator turns a syntax error into a normal
validation error. This module has no project imports on purpose.
"""

from __future__ import annotations

import re
from typing import Any, Final

__all__ = [
    "MAX_POINTER_LENGTH",
    "JsonPointerError",
    "json_type",
    "parse_json_pointer",
    "resolve_json_pointer",
]

#: Upper bound on the pointer string. A retained document is already bounded
#: in depth by the reducer's admission guard, so a longer pointer can never
#: resolve; the cap keeps parse work and error text small.
MAX_POINTER_LENGTH: Final = 1024

#: A ``~`` not followed by ``0`` or ``1`` is invalid (RFC 6901 section 3).
_BAD_ESCAPE: Final = re.compile(r"~(?![01])")

#: An array index: ``0`` or a base-10 number with no leading zero.
_ARRAY_INDEX: Final = re.compile(r"0|[1-9][0-9]*")

#: Longest token echoed back in an error message.
_MAX_TOKEN_ECHO: Final = 64


class JsonPointerError(ValueError):
    """A JSON Pointer is malformed or does not resolve against the document."""


def _echo(token: str) -> str:
    """Return a bounded ``repr`` of *token* for an error message."""
    if len(token) <= _MAX_TOKEN_ECHO:
        return repr(token)
    return repr(token[:_MAX_TOKEN_ECHO]) + "..."


def parse_json_pointer(pointer: str) -> list[str]:
    """Split *pointer* into decoded reference tokens.

    Raises :class:`JsonPointerError` when the pointer is too long, does not
    start with ``/`` (unless empty), or has a ``~`` that is not ``~0`` or
    ``~1``.
    """
    if len(pointer) > MAX_POINTER_LENGTH:
        raise JsonPointerError(
            f"path is {len(pointer)} characters long; the limit is {MAX_POINTER_LENGTH}."
        )
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise JsonPointerError(
            f'path must be empty (the whole document) or start with "/", got {_echo(pointer)}.'
        )
    tokens = pointer[1:].split("/")
    for token in tokens:
        if _BAD_ESCAPE.search(token):
            raise JsonPointerError(f'path token {_echo(token)} has a "~" that is not "~0" or "~1".')
    # RFC 6901 section 4: decode ``~1`` first, then ``~0``.
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def resolve_json_pointer(document: Any, tokens: list[str]) -> Any:
    """Return the value *tokens* point at inside *document*.

    Raises :class:`JsonPointerError` when a step names a missing object
    member, an array step is not a valid index (``-``, a leading zero, not a
    number) or is out of range, or a step tries to go inside a scalar.
    """
    node = document
    for depth, token in enumerate(tokens, start=1):
        if isinstance(node, dict):
            if token not in node:
                raise JsonPointerError(
                    f"path step {depth} ({_echo(token)}) does not resolve: "
                    "the object has no such member."
                )
            node = node[token]
        elif isinstance(node, list):
            node = node[_array_index(token, len(node), depth)]
        else:
            raise JsonPointerError(
                f"path step {depth} ({_echo(token)}) does not resolve: "
                f"the value at step {depth - 1} is a {json_type(node)}, not an object or array."
            )
    return node


def _array_index(token: str, length: int, depth: int) -> int:
    """Validate one array step and return it as an index."""
    if token == "-":
        raise JsonPointerError(
            f'path step {depth} is "-", which names the element after the last one '
            "and never resolves."
        )
    if not _ARRAY_INDEX.fullmatch(token):
        raise JsonPointerError(
            f"path step {depth} ({_echo(token)}) is not a valid array index: use a "
            "base-10 number with no leading zeros."
        )
    index = int(token)
    if index >= length:
        raise JsonPointerError(
            f"path step {depth} (index {index}) does not resolve: the array has "
            f"{length} element(s)."
        )
    return index


def json_type(value: Any) -> str:
    """Name the JSON type of *value*: object, array, string, number, boolean, or null."""
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__
