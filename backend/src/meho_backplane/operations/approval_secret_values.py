# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret values of a call that waits for approval, and where MEHO hides them.

A secret value is a value under a secret name, at any depth (the name rule
of :func:`~meho_backplane.broadcast.events.scrub_secret_named_values`), or a
value in a field that the operation marked with
:func:`~meho_backplane.op_classes.declare_secret_field`. A number counts
when the last word of its name names the secret (``password``, not
``password_max_age_days``).

The stored request keeps a copy of the params with the marker
``[REDACTED:param_name]`` in place of each secret value
(:func:`params_with_call_secrets_hidden`). The same values can come back in
other places: the approval card that a connector builds from the params, and
the answer of the approved run (a connector that returns the note it saved).
:func:`hide_call_secret_values` replaces them there. While an approved call
runs, :func:`call_secret_values_bound` keeps its secret values in a context
variable, so the dispatcher hides them in the answer and in the audit row of
the run (:func:`current_call_secret_values`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from meho_backplane.broadcast.events import HIDDEN_PARAM_MARKER, scrub_secret_named_values
from meho_backplane.op_classes import secret_field_paths

__all__ = [
    "CallSecretValues",
    "call_secret_values",
    "call_secret_values_bound",
    "current_call_secret_values",
    "hide_call_secret_values",
    "params_with_call_secrets_hidden",
    "with_marker_at",
]

#: A text this long or longer is also hidden where it is part of a longer text.
#: A shorter one is hidden only where it is the whole value, so a short value
#: such as ``on`` never cuts other words apart.
_MIN_PART_LENGTH = 6


@dataclass(frozen=True)
class CallSecretValues:
    """The secret texts and numbers of one call."""

    texts: frozenset[str] = frozenset()
    numbers: frozenset[int | float] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.texts or self.numbers)

    def __or__(self, other: CallSecretValues) -> CallSecretValues:
        return CallSecretValues(self.texts | other.texts, self.numbers | other.numbers)


_NO_VALUES = CallSecretValues()

_bound_values: ContextVar[CallSecretValues] = ContextVar(
    "approval_call_secret_values", default=_NO_VALUES
)


def with_marker_at(node: Any, path: tuple[str, ...]) -> Any:
    """A copy of *node* with the value at *path* replaced by the marker.

    A path that does not lead to a field (an earlier step hid its parent)
    changes nothing.
    """
    if not isinstance(node, dict) or path[0] not in node:
        return node
    if len(path) == 1:
        return {**node, path[0]: HIDDEN_PARAM_MARKER}
    return {**node, path[0]: with_marker_at(node[path[0]], path[1:])}


def params_with_call_secrets_hidden(params: Mapping[str, Any], *, op_id: str) -> tuple[Any, bool]:
    """*params* with each secret value replaced by the marker; and whether one was found."""
    hidden, found = scrub_secret_named_values(
        params, hide_numbers=True, numbers_need_secret_word=True
    )
    for path in secret_field_paths(op_id, params):
        hidden = with_marker_at(hidden, path)
        found = True
    return hidden, found


def call_secret_values(params: Mapping[str, Any] | None, *, op_id: str) -> CallSecretValues:
    """The secret texts and numbers in *params*: the values the stored copy hides."""
    if not params:
        return _NO_VALUES
    hidden, found = params_with_call_secrets_hidden(params, op_id=op_id)
    if not found:
        return _NO_VALUES
    texts: set[str] = set()
    numbers: set[int | float] = set()
    _collect_hidden(params, hidden, texts, numbers)
    return CallSecretValues(frozenset(texts), frozenset(numbers))


def _collect_hidden(raw: Any, hidden: Any, texts: set[str], numbers: set[int | float]) -> None:
    """Add each leaf of *raw* that *hidden* shows as the marker."""
    if hidden == HIDDEN_PARAM_MARKER and raw != HIDDEN_PARAM_MARKER:
        _collect_leaves(raw, texts, numbers)
    elif isinstance(raw, Mapping) and isinstance(hidden, Mapping):
        for key, value in raw.items():
            _collect_hidden(value, hidden.get(key), texts, numbers)
    elif _is_sequence(raw) and _is_sequence(hidden) and len(raw) == len(hidden):
        for item, other in zip(raw, hidden, strict=True):
            _collect_hidden(item, other, texts, numbers)


def _collect_leaves(node: Any, texts: set[str], numbers: set[int | float]) -> None:
    if isinstance(node, str):
        if node:
            texts.add(node)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (int, float)):
        numbers.add(node)
    elif isinstance(node, Mapping):
        for value in node.values():
            _collect_leaves(value, texts, numbers)
    elif _is_sequence(node):
        for item in node:
            _collect_leaves(item, texts, numbers)


def _is_sequence(node: Any) -> bool:
    return isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray))


def hide_call_secret_values(node: Any, values: CallSecretValues) -> Any:
    """A copy of *node* with each secret value of *values* replaced by the marker.

    A text leaf that is a secret text, or the text of a secret number, becomes
    the marker. A secret text of at least six letters is also cut out of a
    longer text. A number leaf that is a secret number becomes the marker.
    Keys, booleans and ``None`` stay. *node* is not changed.
    """
    if not values:
        return node
    numbers_as_text = frozenset(str(number) for number in values.numbers)
    parts = sorted(
        (text for text in values.texts if len(text) >= _MIN_PART_LENGTH), key=len, reverse=True
    )
    return _hide(node, values, numbers_as_text, parts)


def _hide(
    node: Any, values: CallSecretValues, numbers_as_text: frozenset[str], parts: list[str]
) -> Any:
    if isinstance(node, str):
        if node in values.texts or node in numbers_as_text:
            return HIDDEN_PARAM_MARKER
        for part in parts:
            if part in node:
                node = node.replace(part, HIDDEN_PARAM_MARKER)
        return node
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, (int, float)):
        return HIDDEN_PARAM_MARKER if node in values.numbers else node
    if isinstance(node, Mapping):
        return {key: _hide(value, values, numbers_as_text, parts) for key, value in node.items()}
    if _is_sequence(node):
        return [_hide(item, values, numbers_as_text, parts) for item in node]
    return node


def current_call_secret_values() -> CallSecretValues:
    """The secret values of the approved call that runs now; empty outside one."""
    return _bound_values.get()


@contextmanager
def call_secret_values_bound(values: CallSecretValues) -> Iterator[None]:
    """Keep *values* as the secret values of the approved call that runs inside."""
    token = _bound_values.set(_bound_values.get() | values)
    try:
        yield
    finally:
        _bound_values.reset(token)
