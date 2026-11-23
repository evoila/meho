# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide the secret values of one call wherever the vendor repeats them.

A vendor system can repeat a value of the request in its answer, most often
in an error text. The key-name walk and the Tier-1 shapes find a secret by
its name or its form. They do not know the value MEHO just sent. This module
knows it, and hides it by value.

A call has a set of known secret values (:class:`CallSecrets`), kept in a
context variable for the length of the call:

* the values of the params under a secret name, or under a field the
  operation's schema marks as secret (``writeOnly`` or ``format: password``);
* the values MEHO reads from the secret store for this call (the secret
  fields of a target's secret).

:func:`scrub_call_secrets` and :func:`scrub_call_secrets_tree` replace each
occurrence of such a value in a text, or in every string of an answer, with
:data:`CALL_SECRET_MARKER`. The vendor may repeat a value in a changed form,
so these forms are found too: JSON-escaped (also twice), percent-encoded, and
base64 of the whole value (standard and URL-safe, with and without padding).

A value shorter than :data:`MIN_VALUE_LENGTH` characters is not known: it
would hide parts of ordinary words. Two values where one is a part of the
other, or that overlap, are hidden together: the spans are merged first, so
no rest of either value stays.

A call with no known value does no work: every function returns at once.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from contextvars import ContextVar, Token
from typing import Any, Final

__all__ = [
    "CALL_SECRET_MARKER",
    "MIN_VALUE_LENGTH",
    "CallSecrets",
    "bind_call_secrets",
    "collect_param_secret_values",
    "marked_secret_names",
    "note_call_secrets",
    "reset_call_secrets",
    "scrub_call_secrets",
    "scrub_call_secrets_tree",
]

#: What a known secret value is replaced with.
CALL_SECRET_MARKER: Final[str] = "[REDACTED:call_secret]"

#: A value shorter than this is not hidden by value.
MIN_VALUE_LENGTH: Final[int] = 4

_PERCENT_HEX: Final[re.Pattern[str]] = re.compile(r"%[0-9A-F]{2}")


def _json_forms(value: str) -> set[str]:
    once = {
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
    }
    once |= {form.replace("/", "\\/") for form in once}
    twice = {json.dumps(form)[1:-1] for form in once}
    return once | twice


def _percent_forms(value: str) -> set[str]:
    forms = {
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
        urllib.parse.quote(value, safe="~"),
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

    __slots__ = ("_forms", "_pattern", "_values")

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        self._forms: tuple[str, ...] = ()
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
        self._forms = tuple(sorted(forms, key=len, reverse=True))
        self._pattern = re.compile("|".join(re.escape(form) for form in self._forms))

    def has_value_in(self, text: str) -> bool:
        pattern = self._pattern
        return pattern is not None and pattern.search(text) is not None

    def scrub(self, text: str) -> str:
        """Replace every occurrence of every known form with the marker."""
        if not self.has_value_in(text):
            return text
        spans: list[tuple[int, int]] = []
        for form in self._forms:
            start = text.find(form)
            while start != -1:
                spans.append((start, start + len(form)))
                start = text.find(form, start + 1)
        spans.sort()
        parts: list[str] = []
        cursor = 0
        index = 0
        while index < len(spans):
            begin, end = spans[index]
            index += 1
            while index < len(spans) and spans[index][0] <= end:
                end = max(end, spans[index][1])
                index += 1
            parts.append(text[cursor:begin])
            parts.append(CALL_SECRET_MARKER)
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)

    def scrub_tree(self, node: Any) -> Any:
        """Return *node* with the marker in every string that holds a known value."""
        if isinstance(node, str):
            return self.scrub(node)
        if isinstance(node, Mapping):
            return {key: self.scrub_tree(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.scrub_tree(item) for item in node]
        if isinstance(node, tuple):
            return tuple(self.scrub_tree(item) for item in node)
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


def _scalar(node: Any) -> str | None:
    if isinstance(node, bool) or node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, int | float):
        return str(node)
    return None


def collect_param_secret_values(
    node: Any, *, marked_names: frozenset[str] = frozenset()
) -> Iterable[str]:
    """Yield the values under a secret name, or under a name in *marked_names*.

    Under a secret name, a scalar or a list of scalars is a known value. A
    mapping under any name is walked, and the name rule applies to its keys.
    """
    from meho_backplane.broadcast.events import is_secret_param_name

    def walk(node: Any, secret: bool) -> Iterable[str]:
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = str(key)
                yield from walk(value, name in marked_names or is_secret_param_name(name))
        elif isinstance(node, list | tuple):
            for item in node:
                yield from walk(item, secret)
        elif secret:
            text = _scalar(node)
            if text is not None:
                yield text

    yield from walk(node, False)


def marked_secret_names(schema: Any) -> frozenset[str]:
    """Names of the properties a schema marks as secret (``writeOnly``, ``format: password``)."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                for name, sub in properties.items():
                    if isinstance(sub, Mapping) and (
                        sub.get("writeOnly") is True or sub.get("format") == "password"
                    ):
                        names.add(str(name))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return frozenset(names)
