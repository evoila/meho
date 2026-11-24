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
the secret is), the texts of the other secret name/value pairs, and every
value of a marked field. While an approved call
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

from meho_backplane.broadcast.events import (
    HIDDEN_PARAM_MARKER,
    has_snmp_word,
    is_secret_name_value_pair,
    is_snmp_community_key,
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
    "hide_call_secret_keys",
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
    a name/value pair with a secret name (a number only when the pair's own
    name ends in a secret word); a text under an SNMP ``community``; and
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


def _collect_named(
    node: Any, texts: set[str], numbers: set[int | float], *, in_snmp: bool = False
) -> None:
    """Add each value under a secret name.

    A text or a number under a name whose last word names the secret. The
    ``value`` of a name/value pair with a secret name, also ``community``
    (:func:`~meho_backplane.broadcast.events.is_secret_name_value_pair`):
    a text always, a number only when the pair's own name ends in a secret
    word, so ``{"name": "password", "value": 4711}`` counts and
    ``{"name": "password_max_age_days", "value": 90}`` does not. A text
    under a bare ``community`` in an SNMP place
    (:func:`~meho_backplane.broadcast.events.is_snmp_community_key`).
    These are the values the stored request hides.
    """
    if isinstance(node, Mapping):
        pair = is_secret_name_value_pair(node)
        for key, value in node.items():
            name = str(key)
            if pair and name == "value":
                pair_numbers = numbers if name_ends_in_secret_word(str(node["name"])) else None
                _add_secret(value, texts, pair_numbers)
            elif name_ends_in_secret_word(name):
                _add_secret(value, texts, numbers)
            elif is_snmp_community_key(node, name, in_snmp=in_snmp):
                _add_secret(value, texts, None)
            _collect_named(value, texts, numbers, in_snmp=in_snmp or has_snmp_word(name))
    elif _is_sequence(node):
        for item in node:
            _collect_named(item, texts, numbers, in_snmp=in_snmp)


def _add_secret(value: Any, texts: set[str], numbers: set[int | float] | None) -> None:
    """Add a secret value: a text, a number, or each of them in a list.

    With *numbers* ``None`` only texts are added. The other fields of an
    object under a secret name are not added: the walk looks at their own
    names.
    """
    if _is_sequence(value):
        for item in value:
            _add_secret(item, texts, numbers)
    else:
        _add_scalar(value, texts, numbers)


def _add_scalar(value: Any, texts: set[str], numbers: set[int | float] | None) -> None:
    if isinstance(value, str):
        if value and value != HIDDEN_PARAM_MARKER:
            texts.add(value)
    elif numbers is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
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
    longer text (``Saved note: <note>``) and in a key of an object; the
    words around it stay. Two keys that become the same text keep apart: the
    second gets a number (``[REDACTED:param_name] (2)``). A secret number is
    replaced as a number leaf, and in a text where it stands as a number of
    its own (not as part of a longer number). Booleans and ``None`` stay.
    *node* is not changed.
    """
    if not values:
        return node
    return _hide(node, _Matcher.of(values), keys_only=False)


def hide_call_secret_keys(node: Any, values: CallSecretValues) -> Any:
    """A copy of *node* with each secret value of *values* replaced in its keys only.

    For the audit row's raw copy: its values already hold a fingerprint
    where the answer hides a value, and a fingerprint must stay as it is.
    """
    if not values:
        return node
    return _hide(node, _Matcher.of(values), keys_only=True)


def hide_call_secret_values_on_card(
    proposed_effect: dict[str, Any] | None, values: CallSecretValues
) -> dict[str, Any] | None:
    """*proposed_effect* with the secret values hidden in the parts built from the params.

    The fields MEHO sets itself (:data:`MEHO_CARD_FIELDS`: the risk level,
    the operation, the classification, the requester's login client) keep
    their value, and so does the requester record in
    ``permission_preflight``. So the approval rules read the card as it was
    built, and an approvals surface can still check the agent's grants.
    """
    if proposed_effect is None or not values:
        return proposed_effect
    matcher = _Matcher.of(values)
    hidden: dict[str, Any] = {}
    for key, value in proposed_effect.items():
        if key in MEHO_CARD_FIELDS:
            hidden[key] = value
        elif key == _PREFLIGHT_KEY and isinstance(value, dict):
            hidden[key] = {
                name: part if name == _REQUESTER_KEY else _hide(part, matcher, keys_only=False)
                for name, part in value.items()
            }
        else:
            hidden[key] = _hide(value, matcher, keys_only=False)
    return hidden


#: The fields of an approval card that MEHO sets from its catalog or from
#: the requester's login, not from the params of the call.
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
        "client",
    }
)

_PREFLIGHT_KEY = "permission_preflight"
_REQUESTER_KEY = "requester"


@dataclass(frozen=True)
class _Matcher:
    """Finds the secret values of one call in a text.

    Built for one masking and dropped after it: no copy of a secret stays
    in a shared cache. The marker is found first and kept, so a secret that
    is part of the marker text does not change it. At one place, a longer
    secret wins over a shorter one.
    """

    needles: tuple[tuple[str, bool], ...]
    numbers: frozenset[int | float]

    @classmethod
    def of(cls, values: CallSecretValues) -> _Matcher:
        texts = sorted(values.texts, key=len, reverse=True)
        numbers = sorted((str(number) for number in values.numbers), key=len, reverse=True)
        needles = [(HIDDEN_PARAM_MARKER, False), *((text, False) for text in texts)]
        needles += [(number, True) for number in numbers]
        return cls(tuple(needles), values.numbers)

    def sub(self, text: str) -> str:
        found: list[tuple[int, int, int]] = []
        for rank, (needle, is_number) in enumerate(self.needles):
            start = text.find(needle)
            while start != -1:
                end = start + len(needle)
                if not is_number or _number_stands_alone(text, start, end):
                    found.append((start, rank, end))
                start = text.find(needle, start + 1)
        if not found:
            return text
        parts: list[str] = []
        position = 0
        for start, rank, end in sorted(found):
            if start < position:
                continue
            parts.append(text[position:start])
            parts.append(text[start:end] if rank == 0 else HIDDEN_PARAM_MARKER)
            position = end
        parts.append(text[position:])
        return "".join(parts)


def _number_stands_alone(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is not part of a longer number."""
    if start > 0 and (text[start - 1].isdigit() or text[start - 1] == "."):
        return False
    if end < len(text) and text[end].isdigit():
        return False
    return not (end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit())


def _hide(node: Any, matcher: _Matcher, *, keys_only: bool) -> Any:
    if isinstance(node, str):
        return node if keys_only else matcher.sub(node)
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, (int, float)):
        return HIDDEN_PARAM_MARKER if not keys_only and node in matcher.numbers else node
    if isinstance(node, Mapping):
        return _hide_mapping(node, matcher, keys_only=keys_only)
    if _is_sequence(node):
        return [_hide(item, matcher, keys_only=keys_only) for item in node]
    return node


def _hide_mapping(node: Mapping[Any, Any], matcher: _Matcher, *, keys_only: bool) -> dict[Any, Any]:
    """Hide the values and the keys of *node*; a hidden key never takes the place of another."""
    renamed = [
        (matcher.sub(key) if isinstance(key, str) else key, key, value)
        for key, value in node.items()
    ]
    kept = {key for new, key, _ in renamed if new == key}
    hidden: dict[Any, Any] = {}
    for new, key, value in renamed:
        if new != key:
            new = _free_key(new, kept, hidden)
        hidden[new] = _hide(value, matcher, keys_only=keys_only)
    return hidden


def _free_key(base: str, kept: set[Any], used: Mapping[Any, Any]) -> str:
    candidate, number = base, 2
    while candidate in kept or candidate in used:
        candidate, number = f"{base} ({number})", number + 1
    return candidate


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
