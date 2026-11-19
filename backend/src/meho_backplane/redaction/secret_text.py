# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide secret-named fields that stand inside a text value of an answer.

An answer often carries a whole file as one string: a config file in a
ConfigMap, the ``kubectl.kubernetes.io/last-applied-configuration`` note,
the Helm values of a custom resource. A secret-named field inside such a
text has no key of its own in the answer, so the key-name walk of the
default view does not see it. This module hides it in the text.

:func:`hide_secret_text` runs two steps over one string:

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
   as YAML. A string value inside the YAML is read again as text,
   so a YAML file inside a JSON note is covered too; such a value is written
   back as a JSON string. Text that is not YAML keeps what step 1 made of it.

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

#: How deep a text inside a text is read again.
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


def _is_snmp_community(names: list[str], name: str, *, in_snmp: bool) -> bool:
    from meho_backplane.broadcast.events import is_snmp_community_key

    return is_snmp_community_key(dict.fromkeys(names), name, in_snmp=in_snmp)


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
        self.seen: set[int] = set()

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
        if key is not None and key.end_mark.index <= start:
            start, new = key.end_mark.index, ": " + new
        self.spans.append((start, end, new))

    def walk(self, node: Any, *, in_snmp: bool = False) -> None:
        if id(node) in self.seen:
            return
        self.seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            self._walk_mapping(node, in_snmp=in_snmp)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                self.walk(item, in_snmp=in_snmp)
        elif isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:str":
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
        names = list(entries)
        for key, value in node.value:
            name = _key_name(key)
            if name is not None and (
                _is_secret_name(name)
                or _is_snmp_community(names, name, in_snmp=in_snmp)
                or (pair and name == "value")
            ):
                self.seen.add(id(value))
                if not _nothing_to_hide(value):
                    self._replace(value, _QUOTED_MARKER, key=key)
            else:
                self.walk(value, in_snmp=in_snmp or (name is not None and _has_snmp_word(name)))


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
    if depth > _MAX_TEXT_DEPTH or ":" not in text or not _MAY_HOLD_SECRET_NAME.search(text):
        return text
    finder = _Finder(text, depth)
    try:
        # Keep every document alive while walking: the walk marks nodes by
        # id(), and a freed document lets a later node reuse an id.
        documents = list(yaml.compose_all(text, Loader=_LOADER))
        for document in documents:
            if document is not None:
                finder.walk(document)
    except yaml.YAMLError:
        return text
    except RecursionError:
        # Too deep to read: hide the whole text rather than show it unread.
        return SECRET_FIELD_MARKER
    return _apply(text, finder.spans) if finder.spans else text


def _hide_text(text: str, depth: int) -> str:
    return _hide_yaml(scrub_secret_fields_in_text(text), depth)


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
