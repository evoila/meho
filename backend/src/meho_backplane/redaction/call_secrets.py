# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide the secret values of one call wherever the vendor repeats them.

A vendor system can repeat a value of the request in its answer, most often
in an error text. The key-name walk and the Tier-1 shapes find a secret by
its name or its form. They do not know the value MEHO just sent. This module
knows it, and hides it by value.

A call has a set of known secret values (:class:`CallSecrets`), kept in a
context variable for the length of the call:

* the values of the params under a secret name, in the ``value`` of a
  name/value pair with a secret name, or under a field the operation's
  schema marks as secret (``writeOnly`` or ``format: password``) at that
  field's own place in the params;
* the values MEHO reads from the secret store for this call (the secret
  fields of a target's secret, under the names the connector reads).

:func:`scrub_call_secrets` and :func:`scrub_call_secrets_tree` replace each
occurrence of such a value in a text, or in every string, key and number of
an answer, with :data:`CALL_SECRET_MARKER`. The vendor may repeat a value in
a changed form, so these forms are found too: JSON-escaped (also twice),
percent-encoded (also with a plain ``/``), and base64 of the whole value
(standard and URL-safe, with and without padding).

A value shorter than :data:`MIN_VALUE_LENGTH` characters is not known: it
would hide parts of ordinary words. Two values where one is a part of the
other, or that overlap, are hidden together: one scan finds every place where
a form starts and merges the spans as it goes, so no rest of either value
stays.

The search pattern is built without Python's pattern cache, so the values
leave memory with the call. A call with no known value does no work: every
function returns at once.
"""

from __future__ import annotations

import base64
import importlib
import json
import re
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar, Token
from typing import Any, Final

__all__ = [
    "CALL_SECRET_MARKER",
    "MIN_VALUE_LENGTH",
    "CallSecrets",
    "bind_call_secrets",
    "collect_param_secret_values",
    "marked_secret_paths",
    "note_call_secrets",
    "reset_call_secrets",
    "scrub_call_secrets",
    "scrub_call_secrets_tree",
]

#: What a known secret value is replaced with.
CALL_SECRET_MARKER: Final[str] = "[REDACTED:call_secret]"

#: A value shorter than this is not hidden by value.
MIN_VALUE_LENGTH: Final[int] = 4

#: A path step that stands for any key (a schema's ``additionalProperties``).
ANY_KEY: Final[str] = "*"

_PERCENT_HEX: Final[re.Pattern[str]] = re.compile(r"%[0-9A-F]{2}")

# ``re.compile`` keeps every pattern in a process-wide cache. The compiler
# under it builds the same pattern object without that cache, so the pattern
# (which holds the values) is freed with the call.
_compile_uncached: Callable[[str, int], re.Pattern[str]] = importlib.import_module(
    "re._compiler"
).compile


def _json_forms(value: str) -> set[str]:
    once = {json.dumps(value, ensure_ascii=ascii_only)[1:-1] for ascii_only in (True, False)}
    once |= {form.replace("/", "\\/") for form in once}
    twice = {
        json.dumps(form, ensure_ascii=ascii_only)[1:-1]
        for form in once
        for ascii_only in (True, False)
    }
    return once | twice


def _percent_forms(value: str) -> set[str]:
    forms = {
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote(value, safe="/"),
        urllib.parse.quote(value, safe="~"),
        urllib.parse.quote_plus(value),
        urllib.parse.quote_plus(value, safe="/"),
    }
    return forms | {_PERCENT_HEX.sub(lambda hit: hit.group().lower(), form) for form in forms}


def _base64_forms(value: str) -> set[str]:
    data = value.encode("utf-8")
    padded = {
        base64.b64encode(data).decode("ascii"),
        base64.urlsafe_b64encode(data).decode("ascii"),
    }
    return padded | {form.rstrip("=") for form in padded}


def _forms_of(value: str) -> set[str]:
    forms = {value} | _json_forms(value) | _percent_forms(value) | _base64_forms(value)
    return {form for form in forms if len(form) >= MIN_VALUE_LENGTH}


class CallSecrets:
    """The known secret values of one call, with their forms and one search pattern."""

    __slots__ = ("_pattern", "_values")

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self.add_all(values)

    def __bool__(self) -> bool:
        return bool(self._values)

    @property
    def values(self) -> frozenset[str]:
        return frozenset(self._values)

    def add_all(self, values: Iterable[str]) -> None:
        fresh = {
            value
            for value in values
            if len(value) >= MIN_VALUE_LENGTH and value not in self._values
        }
        if not fresh:
            return
        self._values |= fresh
        forms: set[str] = set()
        for value in self._values:
            forms |= _forms_of(value)
        # Longest first: at each place the alternation takes the longest form.
        ordered = sorted(forms, key=len, reverse=True)
        alternation = "|".join(re.escape(form) for form in ordered)
        # A look-ahead matches at every place where a form starts, also inside
        # an earlier hit, so overlapping values are found in the same scan.
        self._pattern = _compile_uncached(f"(?=({alternation}))", 0)

    def _spans(self, text: str) -> Iterator[tuple[int, int]]:
        """Yield the merged spans of every known form in *text*, in one scan."""
        pattern = self._pattern
        if pattern is None:
            return
        begin = end = -1
        for hit in pattern.finditer(text):
            start = hit.start()
            stop = start + len(hit.group(1))
            if start <= end:
                end = max(end, stop)
                continue
            if end != -1:
                yield begin, end
            begin, end = start, stop
        if end != -1:
            yield begin, end

    def scrub(self, text: str) -> str:
        """Replace every occurrence of every known form with the marker."""
        parts: list[str] = []
        cursor = 0
        for begin, end in self._spans(text):
            parts.append(text[cursor:begin])
            parts.append(CALL_SECRET_MARKER)
            cursor = end
        if not parts:
            return text
        parts.append(text[cursor:])
        return "".join(parts)

    def _scrub_key(self, key: Any, taken: Mapping[Any, Any]) -> Any:
        if not isinstance(key, str):
            return key
        clean = self.scrub(key)
        if clean == key:
            return key
        unique, count = clean, 1
        while unique in taken:
            count += 1
            unique = f"{clean}#{count}"
        return unique

    def scrub_tree(self, node: Any) -> Any:
        """Return *node* with the marker for every known value in a string, key or number."""
        if isinstance(node, str):
            return self.scrub(node)
        if isinstance(node, Mapping):
            out: dict[Any, Any] = {}
            for key, value in node.items():
                out[self._scrub_key(key, out)] = self.scrub_tree(value)
            return out
        if isinstance(node, list):
            return [self.scrub_tree(item) for item in node]
        if isinstance(node, tuple):
            return tuple(self.scrub_tree(item) for item in node)
        if isinstance(node, int | float) and not isinstance(node, bool):
            text = str(node)
            clean = self.scrub(text)
            return node if clean == text else clean
        return node


_call_secrets_var: ContextVar[CallSecrets | None] = ContextVar("call_secrets", default=None)


def bind_call_secrets(values: Iterable[str]) -> Token[CallSecrets | None]:
    """Open the secret set of one call, seeded with the set of the call around it.

    A step of a multi-step operation starts with the values of its parent.
    Give the token to :func:`reset_call_secrets` when the call ends.
    """
    parent = _call_secrets_var.get()
    scope = CallSecrets(parent.values if parent is not None else ())
    scope.add_all(values)
    return _call_secrets_var.set(scope)


def reset_call_secrets(token: Token[CallSecrets | None]) -> None:
    _call_secrets_var.reset(token)


def note_call_secrets(values: Iterable[str]) -> None:
    """Add values the call has just read from the secret store. No-op outside a call."""
    scope = _call_secrets_var.get()
    if scope is not None:
        scope.add_all(values)


def scrub_call_secrets(text: str) -> str:
    """:meth:`CallSecrets.scrub` for the call that runs now. No-op outside a call."""
    scope = _call_secrets_var.get()
    return scope.scrub(text) if scope else text


def scrub_call_secrets_tree(node: Any) -> Any:
    """:meth:`CallSecrets.scrub_tree` for the call that runs now. No-op outside a call."""
    scope = _call_secrets_var.get()
    return scope.scrub_tree(node) if scope else node


def _scalars(node: Any) -> Iterator[str]:
    """A scalar, or each scalar of a list, as text. ``None`` and booleans are no value."""
    items = node if isinstance(node, list | tuple) else (node,)
    for item in items:
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, str):
            yield item
        elif isinstance(item, int | float):
            yield str(item)


def _on_marked_path(path: tuple[str, ...], marked: frozenset[tuple[str, ...]]) -> bool:
    return any(
        len(mark) == len(path)
        and all(step in (ANY_KEY, key) for step, key in zip(mark, path, strict=True))
        for mark in marked
    )


def collect_param_secret_values(
    node: Any, *, marked_paths: frozenset[tuple[str, ...]] = frozenset()
) -> Iterator[str]:
    """Yield the secret values of the params *node*.

    A scalar, or a list of scalars, is a known value under a secret name,
    as the ``value`` of a name/value pair with a secret name, or at a path in
    *marked_paths* (the keys from the top of the params; a list adds no
    step). A mapping under any name is walked, and the rules apply to its
    keys.
    """
    from meho_backplane.broadcast.events import is_secret_name_value_pair, is_secret_param_name

    def walk(node: Any, path: tuple[str, ...], secret: bool) -> Iterator[str]:
        if isinstance(node, Mapping):
            if is_secret_name_value_pair(node):
                yield from _scalars(node["value"])
            for key, value in node.items():
                name = str(key)
                inner = (*path, name)
                hidden = is_secret_param_name(name) or _on_marked_path(inner, marked_paths)
                yield from walk(value, inner, hidden)
        elif isinstance(node, list | tuple):
            for item in node:
                yield from walk(item, path, secret)
        elif secret:
            yield from _scalars(node)

    yield from walk(node, (), False)


_COMBINERS: Final = ("allOf", "anyOf", "oneOf")


def _resolve_local_ref(root: Any, ref: Any) -> Any:
    if not (isinstance(ref, str) and ref.startswith("#/")):
        return None
    node = root
    for part in ref[2:].split("/"):
        step = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or step not in node:
            return None
        node = node[step]
    return node


def _marks_secret(node: Any) -> bool:
    return isinstance(node, Mapping) and (
        node.get("writeOnly") is True or node.get("format") == "password"
    )


def _children(
    node: Mapping[str, Any], path: tuple[str, ...]
) -> Iterator[tuple[Any, tuple[str, ...]]]:
    """Each sub-schema of *node* with its path: properties, items, combiners, extra keys."""
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for name, sub in properties.items():
            yield sub, (*path, str(name))
    for key in ("items", "prefixItems", *_COMBINERS):
        subs = node.get(key)
        for sub in subs if isinstance(subs, list) else (subs,):
            yield sub, path
    extra = node.get("additionalProperties")
    if isinstance(extra, Mapping):
        yield extra, (*path, ANY_KEY)


def marked_secret_paths(schema: Any) -> frozenset[tuple[str, ...]]:
    """The paths of the properties a schema marks as secret (``writeOnly``, ``format: password``).

    A path is the property names from the top of the schema. An array item
    adds no step, ``additionalProperties`` adds :data:`ANY_KEY`, and a local
    ``$ref`` is followed (each one once per path, so a schema that refers to
    itself ends).
    """
    paths: set[tuple[str, ...]] = set()

    def walk(node: Any, path: tuple[str, ...], seen: frozenset[str]) -> None:
        if not isinstance(node, Mapping):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in seen:
            walk(_resolve_local_ref(schema, ref), path, seen | {ref})
        for sub, inner in _children(node, path):
            if inner != path and _marks_secret(sub):
                paths.add(inner)
            walk(sub, inner, seen)

    walk(schema, (), frozenset())
    return frozenset(paths)
