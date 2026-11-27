# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read a text that is a JSON object or list, and hide values in it in place.

:func:`load_json_text` parses the text and refuses one MEHO cannot read
safely: one too deep to parse, or one with a map that names a key twice
(the parsed map keeps only the last value, but the text holds every value).

:func:`replace_changed_values` writes the hidden values back into the text
itself. Each value that changed is replaced where it stands; all other text
(the keys, their order, the spacing, the line breaks) stays as it was.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Final

__all__ = [
    "UnreadableTextError",
    "load_json_text",
    "reject_repeated_keys",
    "replace_changed_values",
]

_SPACE = re.compile(r"[ \t\n\r]*")

_DECODER: Final[json.JSONDecoder] = json.JSONDecoder()


class UnreadableTextError(ValueError):
    """A JSON or YAML text MEHO cannot read safely: too deep, or a key named twice in a map."""


def reject_repeated_keys(keys: Iterable[Any]) -> None:
    """Raise :class:`UnreadableTextError` when a key comes twice in *keys*."""
    seen: set[Any] = set()
    for key in keys:
        if key in seen:
            raise UnreadableTextError(f"key {key!r} is repeated")
        seen.add(key)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    reject_repeated_keys(key for key, _ in pairs)
    return dict(pairs)


def load_json_text(text: str) -> Any:
    """The parsed JSON of *text*.

    Raises :class:`UnreadableTextError` when the text is too deep to parse
    or a map in it names one key twice, and :class:`ValueError` when the
    text is not JSON.
    """
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs)
    except RecursionError as exc:
        raise UnreadableTextError("too deep") from exc


def _skip_space(text: str, pos: int) -> int:
    match = _SPACE.match(text, pos)
    return match.end() if match is not None else pos


def _patch_members(
    text: str, pos: int, original: dict[str, Any], changed: dict[str, Any], edits: list[Any]
) -> int:
    """Walk the members of the map that starts at *pos*; return where it ends."""
    pos = _skip_space(text, pos + 1)
    while text[pos] != "}":
        key, pos = _DECODER.raw_decode(text, pos)
        pos = _skip_space(text, _skip_space(text, pos) + 1)
        pos = _skip_space(text, _patch(text, pos, original[key], changed[key], edits))
        if text[pos] == ",":
            pos = _skip_space(text, pos + 1)
    return pos + 1


def _patch_items(
    text: str, pos: int, original: list[Any], changed: list[Any], edits: list[Any]
) -> int:
    """Walk the items of the list that starts at *pos*; return where it ends."""
    pos = _skip_space(text, pos + 1)
    for old_item, new_item in zip(original, changed, strict=True):
        pos = _skip_space(text, _patch(text, pos, old_item, new_item, edits))
        if text[pos] == ",":
            pos = _skip_space(text, pos + 1)
    return pos + 1


def _patch(text: str, pos: int, original: Any, changed: Any, edits: list[Any]) -> int:
    """Note the edits that turn the value at *pos* into *changed*; return where it ends."""
    if original == changed:
        return int(_DECODER.raw_decode(text, pos)[1])
    if isinstance(original, dict) and isinstance(changed, dict) and list(original) == list(changed):
        return _patch_members(text, pos, original, changed, edits)
    if isinstance(original, list) and isinstance(changed, list) and len(original) == len(changed):
        return _patch_items(text, pos, original, changed, edits)
    end = int(_DECODER.raw_decode(text, pos)[1])
    edits.append((pos, end, json.dumps(changed, ensure_ascii=False)))
    return end


def replace_changed_values(text: str, original: Any, changed: Any) -> str:
    """*text* with each value where *changed* differs from *original* replaced in place.

    *original* is :func:`load_json_text` of *text*; *changed* is a copy of
    it with the same keys in each map and the same length of each list,
    except where a whole value was replaced. Raises
    :class:`UnreadableTextError` when the text is too deep to walk.
    """
    edits: list[tuple[int, int, str]] = []
    try:
        _patch(text, _skip_space(text, 0), original, changed, edits)
    except RecursionError as exc:
        raise UnreadableTextError("too deep") from exc
    for start, end, value in reversed(edits):
        text = f"{text[:start]}{value}{text[end:]}"
    return text
