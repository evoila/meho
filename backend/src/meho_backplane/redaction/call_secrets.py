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

:func:`keep_call_secrets_whole` keeps a known value whole when an earlier
rule changed a text that holds it: the text keeps everything that rule hid,
and each known value with what touches it becomes one marker.

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
import difflib
import importlib
import json
import re
import urllib.parse
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "CALL_SECRET_MARKER",
    "MIN_VALUE_LENGTH",
    "AnyItem",
    "CallSecrets",
    "KeyPattern",
    "OtherKey",
    "PathStep",
    "bind_call_secrets",
    "collect_param_secret_values",
    "keep_call_secrets_whole",
    "marked_secret_paths",
    "note_call_secrets",
    "reset_call_secrets",
    "scrub_call_secret_path",
    "scrub_call_secrets",
    "scrub_call_secrets_tree",
]

#: What a known secret value is replaced with.
CALL_SECRET_MARKER: Final[str] = "[REDACTED:call_secret]"

#: A value shorter than this is not hidden by value.
MIN_VALUE_LENGTH: Final[int] = 4


#: Two texts with more words than this together are not compared word by
#: word: a text that holds a known value is then hidden whole.
_DIFF_LIMIT: Final[int] = 20_000

#: The words and the white space between them, in order.
_WORDS: Final[re.Pattern[str]] = re.compile(r"\s+|\S+")


@dataclass(frozen=True, slots=True)
class OtherKey:
    """A path step for a key no named property or name pattern takes (``additionalProperties``)."""

    declared: frozenset[str]
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KeyPattern:
    """A path step for a key that matches a name pattern (``patternProperties``)."""

    pattern: str


@dataclass(frozen=True, slots=True)
class AnyItem:
    """A path step for each array item from position *start* on (``items``)."""

    start: int = 0


#: One step of a path in the params: a key, an array position, or a rule of
#: the schema that takes several keys or positions.
PathStep = str | int | OtherKey | KeyPattern | AnyItem

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
    # An encoder that writes ``/`` as ``\/`` does so in each round.
    twice |= {form.replace("/", "\\/") for form in twice}
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


def _touching_groups(
    items: list[tuple[int, int, str | None]],
) -> Iterator[list[tuple[int, int, str | None]]]:
    """The sorted *items* in groups that overlap or touch (one starts where another ends)."""
    group: list[tuple[int, int, str | None]] = []
    end = -1
    for item in items:
        if group and item[0] > end:
            yield group
            group = []
        group.append(item)
        end = max(end, item[1]) if len(group) > 1 else item[1]
    if group:
        yield group


def _shares_a_piece(value: str, text: str) -> bool:
    """True when *text* holds any :data:`MIN_VALUE_LENGTH` characters in a row of *value*."""
    size = MIN_VALUE_LENGTH
    return any(value[start : start + size] in text for start in range(len(value) - size + 1))


def _group_text(original: str, group: list[tuple[int, int, str | None]]) -> str:
    """What one group shows: the rule's text when it hid each value whole, else the marker.

    An item with ``None`` as its text is a known value; every other item is
    a change of a rule with the text it wrote. The rule hid a value whole
    when one change holds the value and the rule's text holds no piece of it.
    """
    changes = [(start, end) for start, end, text in group if text is not None]
    values = [original[start:end] for start, end, text in group if text is None]
    written = "".join(text for _, _, text in group if text is not None)
    whole = all(
        any(a <= start and end <= b for a, b in changes)
        for start, end, text in group
        if text is None
    )
    if whole and not any(_shares_a_piece(value, written) for value in values):
        return written
    return CALL_SECRET_MARKER


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

    def _scrub_keys(self, keys: list[Any], keep_names: Collection[str] = ()) -> list[Any]:
        """The keys of one mapping, each known value masked, no two the same.

        A key that stays as it is, or is in *keep_names*, keeps its name. A
        masked key gets a number when its masked name is taken, so no entry
        replaces another.
        """
        cleaned = [
            self.scrub(key) if isinstance(key, str) and key not in keep_names else key
            for key in keys
        ]
        taken = {clean for key, clean in zip(keys, cleaned, strict=True) if clean == key}
        out: list[Any] = []
        for key, clean in zip(keys, cleaned, strict=True):
            if clean != key:
                unique, count = clean, 1
                while unique in taken:
                    count += 1
                    unique = f"{clean}#{count}"
                taken.add(unique)
                clean = unique
            out.append(clean)
        return out

    def scrub_tree(
        self, node: Any, *, keep_keys: bool = False, keep_names: Collection[str] = ()
    ) -> Any:
        """Return *node* with the marker for every known value in a string, key or number.

        With *keep_keys*, the keys of the top mapping stay as they are: they
        are field names MEHO sets itself. A key in *keep_names* stays as it is
        at every depth: it is a field name of MEHO's own answer, so it says
        nothing about a value.
        """
        if isinstance(node, str):
            return self.scrub(node)
        if isinstance(node, Mapping):
            keys = list(node)
            names = keys if keep_keys else self._scrub_keys(keys, keep_names)
            return {
                name: self.scrub_tree(node[key], keep_names=keep_names)
                for name, key in zip(names, keys, strict=True)
            }
        if isinstance(node, list | tuple):
            items = [self.scrub_tree(item, keep_names=keep_names) for item in node]
            return items if isinstance(node, list) else tuple(items)
        if isinstance(node, int | float) and not isinstance(node, bool):
            text = str(node)
            clean = self.scrub(text)
            return node if clean == text else clean
        return node

    def scrub_path(self, tree: Any, path: str) -> str:
        """The dotted *path* into *tree* with each key named as :meth:`scrub_tree` names it.

        A key that holds a known value is masked, and gets the same number as
        in the masked tree, so the path points at the same entry. A path that
        does not resolve in *tree* has its known values masked as text.
        """
        mapped = self._mapped_path(tree, path)
        return self.scrub(path) if mapped is None else mapped

    def _mapped_path(self, node: Any, rest: str) -> str | None:
        if rest == "":
            return ""
        if isinstance(node, Mapping):
            keys = list(node)
            for key, name in zip(keys, self._scrub_keys(keys), strict=True):
                text = str(key)
                if rest == text:
                    return str(name)
                if rest.startswith(f"{text}."):
                    tail = self._mapped_path(node[key], rest[len(text) + 1 :])
                    if tail is not None:
                        return f"{name}.{tail}"
            return None
        if isinstance(node, list | tuple):
            head, dot, tail_text = rest.partition(".")
            if not (head.isdigit() and int(head) < len(node)):
                return None
            if not dot:
                return head
            tail = self._mapped_path(node[int(head)], tail_text)
            return None if tail is None else f"{head}.{tail}"
        return None

    def _changed_spans(self, original: str, redacted: str) -> list[tuple[int, int, str]]:
        """The parts of *original* an earlier rule changed, each with what it wrote there.

        The texts are compared word by word (a word is a run of characters
        that are not white space), so a part is always whole words: a letter
        of a hidden word never lines up with a letter of the rule's label.
        """
        was = _WORDS.findall(original)
        now = _WORDS.findall(redacted)
        if len(was) + len(now) > _DIFF_LIMIT:
            return [(0, len(original), CALL_SECRET_MARKER)]
        starts = [0]
        for word in was:
            starts.append(starts[-1] + len(word))
        matcher = difflib.SequenceMatcher(None, was, now, autojunk=False)
        return [
            (starts[i1], starts[i2], "".join(now[j1:j2]))
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]

    def _keep_text_whole(self, original: str, redacted: str) -> str:
        """*redacted* rebuilt from *original*: each rule's change, and no part of a known value.

        A part of *original* that a rule changed shows what the rule wrote.
        A known value, together with every change that touches it, is one
        group. When the rule hid each known value of the group whole (one
        change holds it, and the rule's text holds no piece of it), the group
        shows what the rule wrote, its own label; else the group becomes one
        marker. Every other part is the same in both texts. So
        the result keeps everything the rule hid and no part of a known value.
        """
        items: list[tuple[int, int, str | None]] = [
            *self._changed_spans(original, redacted),
            *((begin, end, None) for begin, end in self._spans(original)),
        ]
        items.sort(key=lambda item: (item[0], item[1]))
        parts: list[str] = []
        cursor = 0
        for group in _touching_groups(items):
            parts.append(original[cursor : group[0][0]])
            parts.append(_group_text(original, group))
            cursor = max(end for _, end, _ in group)
        parts.append(original[cursor:])
        return "".join(parts)

    def _cut(self, original: Any, redacted: Any) -> bool:
        """True when a rule changed a text that holds a known value."""
        return (
            isinstance(original, str)
            and isinstance(redacted, str)
            and original != redacted
            and next(self._spans(original), None) is not None
        )

    def _any_cut(self, original: Any, redacted: Any) -> bool:
        if isinstance(original, Mapping) and isinstance(redacted, Mapping):
            return any(
                self._any_cut(value, redacted[key])
                for key, value in original.items()
                if key in redacted
            )
        if isinstance(original, list) and isinstance(redacted, list):
            return any(map(self._any_cut, original, redacted))
        return self._cut(original, redacted)

    def _merge(self, original: Any, redacted: Any) -> Any:
        if isinstance(redacted, Mapping):
            if not isinstance(original, Mapping):
                return redacted
            return {key: self._merge(original.get(key), value) for key, value in redacted.items()}
        if isinstance(redacted, list):
            if not (isinstance(original, list) and len(original) == len(redacted)):
                return redacted
            return [self._merge(*items) for items in zip(original, redacted, strict=True)]
        if self._cut(original, redacted):
            return self._keep_text_whole(original, redacted)
        return redacted

    def keep_whole(self, original: Any, redacted: Any) -> Any:
        """Return *redacted* with no known value cut by an earlier rule.

        *redacted* is *original* after rules that change only texts. When a
        rule changed a text that holds a known value, it may have replaced
        only part of the value, or hidden more than the value. Such a text
        keeps every change of the rule, and each known value, with every
        change that touches it, becomes one marker
        (:meth:`_keep_text_whole`). Every other text stays as it is.
        """
        if not self._any_cut(original, redacted):
            return redacted
        return self._merge(original, redacted)


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


def scrub_call_secrets_tree(
    node: Any, *, keep_keys: bool = False, keep_names: Collection[str] = ()
) -> Any:
    """:meth:`CallSecrets.scrub_tree` for the call that runs now. No-op outside a call."""
    scope = _call_secrets_var.get()
    return scope.scrub_tree(node, keep_keys=keep_keys, keep_names=keep_names) if scope else node


def scrub_call_secret_path(tree: Any, path: str) -> str:
    """:meth:`CallSecrets.scrub_path` for the call that runs now. No-op outside a call."""
    scope = _call_secrets_var.get()
    return scope.scrub_path(tree, path) if scope else path


def keep_call_secrets_whole(original: Any, redacted: Any) -> Any:
    """:meth:`CallSecrets.keep_whole` for the call that runs now. No-op outside a call."""
    scope = _call_secrets_var.get()
    return scope.keep_whole(original, redacted) if scope else redacted


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


def _pattern_matches(pattern: str, key: str) -> bool | None:
    """Whether *key* matches the schema name *pattern*; ``None`` when the pattern is not valid."""
    try:
        return re.search(pattern, key) is not None
    except re.error:
        return None


def _step_matches(step: PathStep, key: str | int) -> bool:
    if isinstance(step, AnyItem):
        return isinstance(key, int) and key >= step.start
    if isinstance(step, KeyPattern):
        # A pattern Python cannot read takes every key: a mark there stays a mark.
        return isinstance(key, str) and _pattern_matches(step.pattern, key) is not False
    if isinstance(step, OtherKey):
        return (
            isinstance(key, str)
            and key not in step.declared
            and not any(_pattern_matches(pattern, key) for pattern in step.patterns)
        )
    return step == key and type(step) is type(key)


def _on_marked_path(path: tuple[str | int, ...], marked: frozenset[tuple[PathStep, ...]]) -> bool:
    return any(
        len(mark) == len(path)
        and all(_step_matches(step, key) for step, key in zip(mark, path, strict=True))
        for mark in marked
    )


def collect_param_secret_values(
    node: Any, *, marked_paths: frozenset[tuple[PathStep, ...]] = frozenset()
) -> Iterator[str]:
    """Yield the secret values of the params *node*.

    A scalar, or a list of scalars, is a known value under a secret name,
    as the ``value`` of a name/value pair with a secret name, or at a path in
    *marked_paths* (the keys and array positions from the top of the params).
    A mapping under any name is walked, and the rules apply to its keys.
    """
    from meho_backplane.broadcast.events import is_secret_name_value_pair, is_secret_param_name

    def walk(node: Any, path: tuple[str | int, ...], secret: bool) -> Iterator[str]:
        if isinstance(node, Mapping):
            if is_secret_name_value_pair(node):
                yield from _scalars(node["value"])
            for key, value in node.items():
                name = str(key)
                inner: tuple[str | int, ...] = (*path, name)
                hidden = is_secret_param_name(name) or _on_marked_path(inner, marked_paths)
                yield from walk(value, inner, hidden)
        elif isinstance(node, list | tuple):
            for index, item in enumerate(node):
                inner = (*path, index)
                yield from walk(item, inner, secret or _on_marked_path(inner, marked_paths))
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


def _item_children(
    node: Mapping[str, Any], path: tuple[PathStep, ...]
) -> Iterator[tuple[Any, tuple[PathStep, ...]]]:
    """The array item schemas of *node*: one per position, then one for the rest."""
    prefix = node.get("prefixItems")
    items = node.get("items")
    if not isinstance(prefix, list) and isinstance(items, list):
        # Before JSON Schema 2020-12, a list under ``items`` names each position.
        prefix, items = items, node.get("additionalItems")
    positions = prefix if isinstance(prefix, list) else []
    for index, sub in enumerate(positions):
        yield sub, (*path, index)
    yield items, (*path, AnyItem(len(positions)))


def _children(
    node: Mapping[str, Any], path: tuple[PathStep, ...]
) -> Iterator[tuple[Any, tuple[PathStep, ...]]]:
    """Each sub-schema of *node* with its path: properties, items, combiners, extra keys."""
    properties = node.get("properties")
    declared: frozenset[str] = frozenset()
    if isinstance(properties, Mapping):
        declared = frozenset(str(name) for name in properties)
        for name, sub in properties.items():
            yield sub, (*path, str(name))
    patterns: tuple[str, ...] = ()
    by_pattern = node.get("patternProperties")
    if isinstance(by_pattern, Mapping):
        patterns = tuple(str(pattern) for pattern in by_pattern)
        for pattern, sub in by_pattern.items():
            yield sub, (*path, KeyPattern(str(pattern)))
    yield from _item_children(node, path)
    for key in _COMBINERS:
        subs = node.get(key)
        for sub in subs if isinstance(subs, list) else (subs,):
            yield sub, path
    extra = node.get("additionalProperties")
    if isinstance(extra, Mapping):
        yield extra, (*path, OtherKey(declared, patterns))


def marked_secret_paths(schema: Any) -> frozenset[tuple[PathStep, ...]]:
    """The paths of the properties a schema marks as secret (``writeOnly``, ``format: password``).

    A path is the property names and array positions from the top of the
    schema. A ``prefixItems`` entry adds its position, ``items`` adds an
    :class:`AnyItem` step (every position after the ``prefixItems``),
    ``patternProperties`` adds a :class:`KeyPattern` step, and
    ``additionalProperties`` adds an :class:`OtherKey` step (any key that
    is not a declared property and matches no name pattern next to it). A
    local ``$ref`` is followed (each one once per path, so a schema that
    refers to itself ends). The mark is read on the schema at each path, also when it comes
    through a ``$ref``, an array's ``items`` or a combiner.
    """
    paths: set[tuple[PathStep, ...]] = set()

    def walk(node: Any, path: tuple[PathStep, ...], seen: frozenset[str]) -> None:
        if not isinstance(node, Mapping):
            return
        if path and _marks_secret(node):
            paths.add(path)
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in seen:
            walk(_resolve_local_ref(schema, ref), path, seen | {ref})
        for sub, inner in _children(node, path):
            walk(sub, inner, seen)

    walk(schema, (), frozenset())
    return frozenset(paths)
