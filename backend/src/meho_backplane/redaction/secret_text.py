# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret-named fields that stand inside a text value of an answer.

An answer often carries a whole file as one string: a config file in a
ConfigMap, the ``kubectl.kubernetes.io/last-applied-configuration`` note,
the Helm values of a custom resource. A secret-named field inside such a
text has no key of its own in the answer, so the key-name walk of the
default view does not see it. This module hides it in the text.

:func:`hide_secret_text` runs four steps over one string:

1. :func:`~meho_backplane.redaction.secret_fields.scrub_secret_fields_in_text`:
   quoted pairs, as in JSON (``"password": "..."``), Python text and escaped
   JSON inside a JSON string.
2. A YAML step: the text is read as YAML (JSON is YAML too) with the PyYAML
   composer, which keeps the position of every value in the text. The value
   of each secret-named key is replaced in the text by the quoted marker, and
   so is the ``value`` of a name/value pair whose ``name`` is a secret name
   (``- name: DB_PASSWORD`` / ``value: ...``, the env list of a container).
   This covers keys without quotes (``dbPassword: ...``) and block values
   (``password: |``). Like the key-name walk, a bare ``community`` in an
   SNMP place is hidden, and the ``value`` of a pair is hidden whatever its
   type. A hidden value is written as ``key: "<marker>"`` on one line, with
   its anchor and the white space after it kept, so the text still reads
   as YAML. An alias of an anchor inside a hidden value gets the marker
   too. An alias that a merge key uses (``<<: *name`` or ``<<: [*name]``)
   and that names a hidden value or a value inside one gets an empty map.
   A map an alias names is checked again in each SNMP place it is used.
   A string value inside the YAML is read again as text, so a YAML file
   inside a JSON note is covered too; such a value is written
   back as a JSON string. A text that YAML reads as one plain value is not
   read again. Text that is not YAML goes to the line step.
   A text deeper than the levels the pass reads, or too deep to parse, is
   hidden whole when it may hold a secret name.
3. A line step (:func:`~meho_backplane.redaction.secret_markup.hide_secret_lines`):
   in a text that reads as YAML, a comment line such as
   ``# dbPassword: ...``; in a text that does not, every ``key: value``
   line.
4. An XML step (:func:`~meho_backplane.redaction.secret_markup.hide_secret_xml`):
   the text of a secret-named element, a secret-named attribute, and the
   ``value`` attribute of an element whose ``name`` or ``key`` is a secret
   name, and the text of such an element; the ``<value>`` child of an
   element whose ``<name>`` child is a secret name. In a JSON text each
   string is read on its own. Each step runs on every text the YAML step
   reads again, too.

A step that is too deep to read hides the whole text.

The name rule is the shared
:func:`~meho_backplane.broadcast.events.is_secret_param_name`. Like the
key-name walk of the default view, numbers under a secret name are hidden;
``true``, ``false`` and ``null`` stay, an empty value stays, and a value an
earlier step already masked (``[REDACTED:...]``) stays.

:func:`hide_secret_text_leaves` runs :func:`hide_secret_text` over every
string of a dict / list answer, and leaves the fields an operation declared
as plain as they are.
"""

from __future__ import annotations

import bisect
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

import yaml

from meho_backplane.redaction.secret_fields import (
    SECRET_FIELD_MARKER,
    _is_placeholder,
    scrub_secret_fields_in_text,
)
from meho_backplane.redaction.secret_markup import hide_secret_lines, hide_secret_xml

__all__ = ["hide_secret_text", "hide_secret_text_leaves"]

#: The YAML loader. The C loader when PyYAML was built with libyaml; both
#: give the same positions (characters of the text).
_LOADER: Final[Any] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: A text without one of these parts holds no secret name, so the YAML step
#: is not run on it. Each name the shared name rule marks has one of them
#: (``password``, ``pwd``, ``secret``, ``token``, ``credential``, ``psk``,
#: ``kubeconfig``, ``community``, ``connection_string`` and every ``*key``
#: pair). A backslash may start an escape that spells a name.
_MAY_HOLD_SECRET_NAME: Final[re.Pattern[str]] = re.compile(
    r"pass|pwd|secret|token|key|cred|psk|communit|connection|kubeconfig|\\",
    re.IGNORECASE,
)

#: Values under a secret name that are no secret.
_KEPT_TAGS: Final[frozenset[str]] = frozenset({"tag:yaml.org,2002:bool", "tag:yaml.org,2002:null"})

#: How deep a text inside a text is read again. A deeper text that may
#: hold a secret name is hidden whole.
_MAX_TEXT_DEPTH: Final[int] = 3

_QUOTED_MARKER: Final[str] = f'"{SECRET_FIELD_MARKER}"'

#: The anchor and tag in front of a node's text (``&name !!str``).
_NODE_PROPERTIES: Final[re.Pattern[str]] = re.compile(r"(?:[&!]\S*\s+)+")


def _is_secret_name(name: str) -> bool:
    # Imported here: the broadcast package imports the redaction package.
    from meho_backplane.broadcast.events import is_secret_param_name

    return is_secret_param_name(name)


def _is_secret_pair(node: Mapping[str, Any]) -> bool:
    from meho_backplane.broadcast.events import is_secret_name_value_pair

    return is_secret_name_value_pair(node)


def _is_snmp_community(entries: Mapping[str, Any], name: str, *, in_snmp: bool) -> bool:
    from meho_backplane.broadcast.events import is_snmp_community_key

    return is_snmp_community_key(entries, name, in_snmp=in_snmp)


def _has_snmp_word(name: str) -> bool:
    from meho_backplane.broadcast.events import has_snmp_word

    return has_snmp_word(name)


def _key_name(node: Any) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _nothing_to_hide(node: Any) -> bool:
    if isinstance(node, yaml.ScalarNode):
        value = node.value
        return node.tag in _KEPT_TAGS or value == "" or _is_placeholder(value)
    if isinstance(node, (yaml.MappingNode, yaml.SequenceNode)):
        return not node.value
    return True


def _is_plain_text(document: Any) -> bool:
    """True when YAML reads the whole document as one plain (unquoted) value.

    Such a value is the text itself, only with its line breaks folded, so
    reading it again finds nothing new. The style is ``None`` from the
    Python loader and ``""`` from the C loader.
    """
    return isinstance(document, yaml.ScalarNode) and not document.style


def _anchors(span: str) -> str:
    """The anchors (``&name``) at the start of a node's text, each with a space.

    A replaced node keeps its anchor, so an alias (``*name``) elsewhere in
    the text still points at it. A tag (``!!binary``) is dropped: the
    marker would not fit it.
    """
    properties = _NODE_PROPERTIES.match(span)
    names = properties.group(0).split() if properties else []
    return "".join(f"{name} " for name in names if name.startswith("&"))


class _Finder:
    """Collect the text spans of a YAML document that are to be replaced."""

    def __init__(self, text: str, depth: int) -> None:
        self.text = text
        self.depth = depth
        self.spans: list[tuple[int, int, str]] = []
        #: The text of each replaced node after its own anchor: anchors
        #: that stand there are gone from the text.
        self.removed: list[tuple[int, int]] = []
        #: Each node is walked once outside and once inside an SNMP place:
        #: a map an alias names can be both.
        self.seen: set[tuple[int, bool]] = set()

    def _replace(self, node: Any, new: str, *, key: Any = None) -> None:
        """Replace the text of *node* by *new*.

        The anchors of the node and the white space after it stay: a block
        value ends with the line breaks and the indent of the next line. The
        value of a mapping entry is replaced from the end of its key on, so
        a block value on the next lines becomes ``key: "<marker>"`` on one
        line. A value that an alias names (its text stands before the key)
        is replaced where its anchor is.
        """
        start, end = node.start_mark.index, node.end_mark.index
        span = self.text[start:end]
        new = _anchors(span) + new + span[len(span.rstrip()) :]
        self.removed.append((start, end))
        if key is not None and key.end_mark.index <= start:
            start, new = key.end_mark.index, ": " + new
        self.spans.append((start, end, new))

    def walk(self, node: Any, *, in_snmp: bool = False) -> None:
        if (id(node), in_snmp) in self.seen:
            return
        self.seen.add((id(node), in_snmp))
        if isinstance(node, yaml.MappingNode):
            self._walk_mapping(node, in_snmp=in_snmp)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                self.walk(item, in_snmp=in_snmp)
        elif (
            isinstance(node, yaml.ScalarNode)
            and node.tag == "tag:yaml.org,2002:str"
            # A text that reads as just this string holds nothing new.
            and node.value.strip() != self.text.strip()
        ):
            inner = _hide_text(node.value, self.depth + 1)
            if inner != node.value:
                self._replace(node, json.dumps(inner, ensure_ascii=False))

    def _walk_mapping(self, node: Any, *, in_snmp: bool) -> None:
        # The ``value`` of a pair is checked whatever its type: a list or a
        # map under a secret ``name`` is hidden whole.
        entries = {
            name: value.value if isinstance(value, yaml.ScalarNode) else value
            for key, value in node.value
            if (name := _key_name(key)) is not None
        }
        pair = _is_secret_pair(entries)
        # Every ``community`` key of one map gets the same answer: the
        # siblings are read once, not once per key.
        community: bool | None = None
        for key, value in node.value:
            name = _key_name(key)
            is_community = False
            if name is not None and name.lower() == "community":
                if community is None:
                    community = _is_snmp_community(entries, name, in_snmp=in_snmp)
                is_community = community
            if name is not None and (
                _is_secret_name(name) or is_community or (pair and name == "value")
            ):
                self.seen.update({(id(value), False), (id(value), True)})
                if not _nothing_to_hide(value):
                    self._replace(value, _QUOTED_MARKER, key=key)
            else:
                self.walk(value, in_snmp=in_snmp or (name is not None and _has_snmp_word(name)))


class _Removed:
    """The replaced spans, for a quick look-up of where an anchor stands."""

    def __init__(self, removed: list[tuple[int, int]]) -> None:
        # Spans that overlap are joined, so each index falls in at most one.
        # Spans that only touch stay apart: an index where one ends and the
        # next starts is inside neither.
        merged: list[list[int]] = []
        for start, end in sorted(removed):
            if merged and start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self.starts = [start for start, _ in merged]
        self.ends = [end for _, end in merged]
        self.exact = {start for start, _ in removed}

    def inside(self, index: int) -> bool:
        """True when *index* stands inside a replaced span, after its start."""
        at = bisect.bisect_left(self.starts, index) - 1
        return at >= 0 and index < self.ends[at]

    def replaced(self, index: int) -> bool:
        """True when *index* is inside a replaced span or starts one."""
        return index in self.exact or self.inside(index)


def _in_merge(stack: list[list[bool]], event: Any) -> bool:
    """Note *event* in its open map or list; True when a merge uses it.

    A merge uses the value of a ``<<`` key and each item of a list that is
    that value. A map entry is ``[next node is a key, the last key is <<]``,
    a list entry is ``[the list is a merge list]``.
    """
    if not stack:
        return False
    top = stack[-1]
    if len(top) == 1:
        return top[0]
    if top[0]:
        top[0] = False
        top[1] = isinstance(event, yaml.ScalarEvent) and event.value == "<<" and event.implicit[0]
        return False
    top[0] = True
    return top[1]


def _orphan_alias_spans(text: str, removed: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Spans that replace each alias whose anchor stands in a hidden value.

    The alias gets the marker, so the text still reads as YAML and the
    value stays hidden. An alias that a merge key uses (``<<: *name``, or
    each alias of a merge list ``<<: [*name]``) gets an empty map, which a
    merge accepts. Such an alias also gets an empty map when its anchor
    names the hidden value itself: that anchor stays, but on the marker,
    which a merge does not accept.
    """
    spans: list[tuple[int, int, str]] = []
    look_up = _Removed(removed)
    #: Per anchor: (stands inside a hidden value, names a hidden value or
    #: stands inside one).
    gone: dict[str, tuple[bool, bool]] = {}
    stack: list[list[bool]] = []
    for event in yaml.parse(text, Loader=_LOADER):
        if isinstance(event, yaml.DocumentStartEvent):
            gone, stack = {}, []
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            stack.pop()
        elif isinstance(event, yaml.NodeEvent):
            merge = _in_merge(stack, event)
            if isinstance(event, yaml.AliasEvent):
                inside, replaced = gone.get(event.anchor, (False, False))
                new = "{}" if merge and replaced else _QUOTED_MARKER if inside else None
                if new is not None:
                    spans.append((event.start_mark.index, event.end_mark.index, new))
                continue
            if event.anchor is not None:
                index = event.start_mark.index
                gone[event.anchor] = (look_up.inside(index), look_up.replaced(index))
            if isinstance(event, yaml.MappingStartEvent):
                stack.append([True, False])
            elif isinstance(event, yaml.SequenceStartEvent):
                stack.append([merge])
    return spans


def _apply(text: str, spans: list[tuple[int, int, str]]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end, new in sorted(spans, key=lambda span: (span[0], -span[1])):
        if start < cursor:
            continue
        parts.append(text[cursor:start])
        parts.append(new)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _hide_yaml(text: str, depth: int) -> str:
    if ":" not in text or not _MAY_HOLD_SECRET_NAME.search(text):
        return text
    if depth > _MAX_TEXT_DEPTH:
        # Deeper than the pass reads: hide the whole text rather than show it unread.
        return SECRET_FIELD_MARKER
    finder = _Finder(text, depth)
    try:
        # Keep every document alive while walking: the walk marks nodes by
        # id(), and a freed document lets a later node reuse an id.
        documents = list(yaml.compose_all(text, Loader=_LOADER))
        for document in documents:
            if document is not None and not _is_plain_text(document):
                finder.walk(document)
        if not finder.spans:
            return hide_secret_lines(text, comments_only=True)
        spans = finder.spans
        if "&" in text:
            spans = spans + _orphan_alias_spans(text, finder.removed)
    except yaml.YAMLError:
        return hide_secret_lines(text, comments_only=False)
    except RecursionError:
        # Too deep to read: hide the whole text rather than show it unread.
        return SECRET_FIELD_MARKER
    return hide_secret_lines(_apply(text, spans), comments_only=True)


def _hide_text(text: str, depth: int) -> str:
    try:
        hidden = _hide_yaml(scrub_secret_fields_in_text(text), depth)
        if "<" in hidden and _MAY_HOLD_SECRET_NAME.search(hidden):
            return hide_secret_xml(hidden)
    except RecursionError:
        # Too deep to read: hide the whole text rather than show it unread.
        return SECRET_FIELD_MARKER
    return hidden


def hide_secret_text(text: str) -> str:
    """Return *text* with the value of each secret-named field in it hidden."""
    return _hide_text(text, 0)


def hide_secret_text_leaves(node: Any, *, plain_names: frozenset[str] = frozenset()) -> Any:
    """Return a copy of *node* with :func:`hide_secret_text` run over every string.

    A dict entry whose key is in *plain_names* (a field the operation
    declared as plain) keeps its whole value.
    """
    if isinstance(node, str):
        return hide_secret_text(node)
    if isinstance(node, Mapping):
        return {
            key: value
            if str(key) in plain_names
            else hide_secret_text_leaves(value, plain_names=plain_names)
            for key, value in node.items()
        }
    if isinstance(node, Sequence) and not isinstance(node, (bytes, bytearray)):
        return [hide_secret_text_leaves(item, plain_names=plain_names) for item in node]
    return node
