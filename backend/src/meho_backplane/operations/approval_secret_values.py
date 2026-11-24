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
:func:`hide_call_secret_values` replaces them there. It looks for the values
of :func:`call_secret_values`: a value under a name whose last word names
the secret (``password``, not ``password_secret_ref``, which names where
the secret is), and every value of a marked field. While an approved call
runs, :func:`call_secret_values_bound` keeps its secret values in a context
variable, so the dispatcher hides them in the answer and in the audit row of
the run (:func:`current_call_secret_values`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from meho_backplane.broadcast.events import (
    HIDDEN_PARAM_MARKER,
    is_secret_name_value_pair,
    name_ends_in_secret_word,
    scrub_secret_named_values,
)
from meho_backplane.op_classes import secret_field_paths

__all__ = [
    "MEHO_CARD_FIELDS",
    "CallSecretValues",
    "call_secret_values",
    "call_secret_values_bound",
    "current_call_secret_values",
    "hide_call_secret_values",
    "hide_call_secret_values_on_card",
    "params_with_call_secrets_hidden",
    "with_marker_at",
]


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
    """The secret texts and numbers in *params* that MEHO hides where they come back.

    A text or a number under a name whose last word names the secret
    (:func:`~meho_backplane.broadcast.events.name_ends_in_secret_word`), at
    any depth, also each item of a list under such a name; the ``value`` of
    a name/value pair whose own name is such a name; and
    every text and number in a field that *op_id* marked. A reference to a
    secret (``password_secret_ref``) and the other fields of an object under
    a secret name are not in it, so the card keeps showing them.
    """
    if not params:
        return _NO_VALUES
    texts: set[str] = set()
    numbers: set[int | float] = set()
    _collect_named(params, texts, numbers)
    for path in secret_field_paths(op_id, params):
        _collect_leaves(_value_at(params, path), texts, numbers)
    return CallSecretValues(frozenset(texts), frozenset(numbers))


def _value_at(node: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        node = node[part]
    return node


def _collect_named(node: Any, texts: set[str], numbers: set[int | float]) -> None:
    """Add each value under a name whose last word names the secret.

    The ``value`` of a name/value pair takes the pair's own name, so
    ``{"name": "password", "value": 4711}`` counts and
    ``{"name": "password_max_age_days", "value": 90}`` does not.
    """
    if isinstance(node, Mapping):
        pair = is_secret_name_value_pair(node)
        for key, value in node.items():
            name = str(node["name"]) if pair and key == "value" else str(key)
            if name_ends_in_secret_word(name):
                _add_secret(value, texts, numbers)
            _collect_named(value, texts, numbers)
    elif _is_sequence(node):
        for item in node:
            _collect_named(item, texts, numbers)


def _add_secret(value: Any, texts: set[str], numbers: set[int | float]) -> None:
    """Add a secret value: a text, a number, or each of them in a list.

    The other fields of an object under a secret name are not added: the
    walk looks at their own names.
    """
    if _is_sequence(value):
        for item in value:
            _add_secret(item, texts, numbers)
    else:
        _add_scalar(value, texts, numbers)


def _add_scalar(value: Any, texts: set[str], numbers: set[int | float]) -> None:
    if isinstance(value, str):
        if value and value != HIDDEN_PARAM_MARKER:
            texts.add(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        numbers.add(value)


def _collect_leaves(node: Any, texts: set[str], numbers: set[int | float]) -> None:
    if isinstance(node, Mapping):
        for value in node.values():
            _collect_leaves(value, texts, numbers)
    elif _is_sequence(node):
        for item in node:
            _collect_leaves(item, texts, numbers)
    else:
        _add_scalar(node, texts, numbers)


def _is_sequence(node: Any) -> bool:
    return isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray))


def hide_call_secret_values(node: Any, values: CallSecretValues) -> Any:
    """A copy of *node* with each secret value of *values* replaced by the marker.

    A secret text is replaced wherever it stands in a text, also inside a
    longer text (``Saved note: <note>``); the words around it stay. A secret
    number is replaced as a number leaf, and in a text where it stands as a
    number of its own (not as part of a longer number). Keys, booleans and
    ``None`` stay. *node* is not changed.
    """
    if not values:
        return node
    return _hide(node, values, _secret_pattern(values))


def hide_call_secret_values_on_card(
    proposed_effect: dict[str, Any] | None, values: CallSecretValues
) -> dict[str, Any] | None:
    """*proposed_effect* with the secret values hidden in the parts built from the params.

    The fields MEHO sets itself (:data:`MEHO_CARD_FIELDS`: the risk level,
    the operation, the classification) keep their value, so the approval
    rules read the card as it was built.
    """
    if proposed_effect is None or not values:
        return proposed_effect
    pattern = _secret_pattern(values)
    return {
        key: value if key in MEHO_CARD_FIELDS else _hide(value, values, pattern)
        for key, value in proposed_effect.items()
    }


#: The fields of an approval card that MEHO sets from its catalog, not from
#: the params of the call.
MEHO_CARD_FIELDS = frozenset(
    {
        "op_id",
        "connector_id",
        "target_id",
        "op_class",
        "safety_level",
        "category",
        "labels",
        "classification_rule",
        "waits_because",
        "credential_wait",
        "preview_populated",
        "preview_failed",
    }
)


def _secret_pattern(values: CallSecretValues) -> re.Pattern[str]:
    """One pattern for every secret in a text; the marker matches itself, so it stays."""
    texts = sorted(values.texts, key=len, reverse=True)
    parts = [re.escape(HIDDEN_PARAM_MARKER), *(re.escape(text) for text in texts)]
    parts += [
        rf"(?<![0-9.]){re.escape(str(number))}(?!\.?[0-9])"
        for number in sorted(values.numbers, key=lambda n: len(str(n)), reverse=True)
    ]
    return re.compile("|".join(parts))


def _hide(node: Any, values: CallSecretValues, pattern: re.Pattern[str]) -> Any:
    if isinstance(node, str):
        return pattern.sub(HIDDEN_PARAM_MARKER, node)
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, (int, float)):
        return HIDDEN_PARAM_MARKER if node in values.numbers else node
    if isinstance(node, Mapping):
        return {key: _hide(value, values, pattern) for key, value in node.items()}
    if _is_sequence(node):
        return [_hide(item, values, pattern) for item in node]
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
