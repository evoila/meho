# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""YAML for :mod:`.api_file_secrets`: parse on the node graph, replace secrets, write back.

A YAML alias (``*name``) points at the same node object as its anchor, so
a secret value is marked on the node graph before the values are built,
and every place that refers to the marked node (the anchor, each alias, a
mapping merged with ``<<``) gets the placeholder. The values are then
built from the changed graph and walked by :class:`Scrubber` like a JSON
file, and the copy is written back by the safe dumper. Comments and
repeated keys are not in the copy: the dumper writes only what the loader
built, so a secret the loader dropped never comes back either.

:func:`check_yaml` serves a YAML file, and :func:`last_check_yaml` runs
the last check on its copy (:mod:`.api_file_last_check`).
:func:`check_yaml_example` serves a YAML example inside an HTML page (a
``<pre>`` or ``<code>`` block). It fails closed: an example it cannot
read whole raises :exc:`WithheldError` with the reason, and the caller
withholds the block. That is an example with more than one document, one
that does not parse, and one that names a key twice in one mapping, at
any depth.
"""

from __future__ import annotations

from typing import Any, Final

import yaml

from meho_backplane.connectors._shared.api_file_last_check import Work, hold_back_in_data
from meho_backplane.connectors._shared.api_file_values import (
    MORE_THAN_ONE_DOCUMENT,
    NOT_PARSED,
    REDACTED,
    REPEATED_KEY,
    TOO_DEEP,
    ApiFileFormatError,
    Scrubber,
    TextRule,
    WithheldError,
    is_secret_name,
)

__all__ = ["check_yaml", "check_yaml_example", "last_check_yaml"]

_YAML_STR_TAG: Final[str] = "tag:yaml.org,2002:str"
_YAML_LOADER: Final[type[yaml.SafeLoader]] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YAML_DUMPER: Final[type[yaml.SafeDumper]] = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

#: What PyYAML's composer says when the text holds a second document.
_SECOND_DOCUMENT: Final[str] = "expected a single document in the stream"


def check_yaml(text: str, path: str, text_rule: TextRule | None) -> tuple[str, Scrubber]:
    """*text*, a YAML file, with every secret value replaced, and the scrubber that counted.

    Raises :exc:`ApiFileFormatError` when the file does not parse or
    cannot be written back; the message names the reason, never the text.
    An empty document comes back empty.
    """
    try:
        root, data, count = _parse(text)
    except RecursionError:
        raise
    except Exception as exc:
        raise ApiFileFormatError(
            f"{path} is not well-formed YAML ({_yaml_reason(exc)}); the file is not returned"
        ) from None
    scrubber = Scrubber(count, text_rule=text_rule, strict=True)
    if root is None:
        return "", scrubber
    copy = scrubber.tree(data, {})
    try:
        return _dump(copy), scrubber
    except yaml.YAMLError as exc:
        raise ApiFileFormatError(
            f"{path} cannot be written back as YAML ({type(exc).__name__}); not returned"
        ) from None


def last_check_yaml(copy: str, scrubber: Scrubber, work: Work) -> str:
    """*copy*, a checked YAML file, with every part that still shows a secret held back.

    The copy is loaded again and walked by :func:`hold_back_in_data`. A
    copy with nothing to hold back comes back as it is; otherwise it is
    written again by the safe dumper.
    """
    if not copy:
        return copy
    before = scrubber.count
    data = hold_back_in_data(yaml.load(copy, Loader=_YAML_LOADER), scrubber, work)
    return copy if scrubber.count == before else _dump(data)


def check_yaml_example(text: str, scrubber: Scrubber) -> tuple[str | None, Any]:
    """*text*, an example in YAML, checked and written back; or the one scalar it is.

    Returns ``(copy, None)`` for a mapping or a list: every secret value
    replaced (counted on *scrubber*) and the copy written back by the
    safe dumper. Returns ``(None, value)`` when the text is one scalar
    (``value``) or holds no document at all (``None``): such text has no
    names, and the caller treats it as plain text.

    Raises :exc:`WithheldError` when the example cannot be checked whole: it
    holds more than one document, it does not parse or its values cannot
    be built, a mapping at any depth names a key twice (the loader would
    keep the last value only), or a key is not a scalar.
    """
    loader = _YAML_LOADER(text)
    try:
        root = _single_node(loader)
        if root is None:
            return None, None
        if isinstance(root, yaml.ScalarNode):
            return None, loader.construct_document(root)
        _refuse_repeated_keys(loader, root, set())
        count = _redact_yaml_nodes(root)
        data = loader.construct_document(root)
    except WithheldError:
        raise
    except RecursionError:
        raise WithheldError(TOO_DEEP) from None
    except Exception:  # any failure to read the example means it cannot be checked
        raise WithheldError(NOT_PARSED) from None
    finally:
        loader.dispose()
    scrubber.count += count
    copy = scrubber.tree(data, {})
    try:
        return _dump(copy), None
    except yaml.YAMLError:
        raise WithheldError(NOT_PARSED) from None


def _single_node(loader: Any) -> yaml.Node | None:
    """The root node of the one document in the text; :exc:`WithheldError` for a second one."""
    try:
        node: yaml.Node | None = loader.get_single_node()
    except yaml.composer.ComposerError as exc:
        if exc.context == _SECOND_DOCUMENT:
            raise WithheldError(MORE_THAN_ONE_DOCUMENT) from None
        raise
    return node


def _refuse_repeated_keys(loader: Any, node: yaml.Node, seen: set[int]) -> None:
    """Raise :exc:`WithheldError` when a mapping under *node* repeats a key or has a non-scalar key.

    The loader builds a mapping by assigning key after key, so a repeated
    key keeps the last value only. Keys are compared as the loader builds
    them (``1`` and ``"1"`` differ, ``yes`` and ``true`` do not). An alias
    stands for a node already seen, which is checked once.
    """
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        keys: set[Any] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise WithheldError(NOT_PARSED)
            key = loader.construct_object(key_node)
            if key in keys:
                raise WithheldError(REPEATED_KEY)
            keys.add(key)
            _refuse_repeated_keys(loader, value_node, seen)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _refuse_repeated_keys(loader, item, seen)


def _parse(text: str) -> tuple[yaml.Node | None, Any, int]:
    """Parse *text* as one document; replace secret values on the node graph; build the values.

    Returns the root node (``None`` for an empty document), the values
    and the count of replaced nodes. Whatever the loader or the value
    construction raises is raised as it is.
    """
    loader = _YAML_LOADER(text)
    try:
        root = loader.get_single_node()
        if root is None:
            return None, None, 0
        count = _redact_yaml_nodes(root)
        return root, loader.construct_document(root), count
    finally:
        loader.dispose()


def _dump(data: Any) -> str:
    text: str = yaml.dump(data, Dumper=_YAML_DUMPER, sort_keys=False, allow_unicode=True)
    return text


def _yaml_reason(exc: Exception) -> str:
    """The kind of YAML error and where it is, never what the file says there.

    PyYAML's ``problem`` text can quote the file (an unknown tag names the
    tag), so only the error class and the position are returned. Building
    the values can fail outside PyYAML's own errors too: a tagged scalar
    that does not fit its tag (``!!int made-up``, ``!!float``,
    ``!!timestamp``, ``!!bool``) raises the error of the stdlib call that
    read it, and that error quotes the value. Such an error has no
    position, so only its class is returned.
    """
    kind = type(exc).__name__
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark or exc.context_mark
        if mark is not None:
            return f"{kind} at line {mark.line + 1}, column {mark.column + 1}"
    return kind


def _redact_yaml_nodes(root: yaml.Node) -> int:
    """Replace the value nodes of secret-named keys in the node graph.

    An alias points at the same node object as its anchor, so marking
    the node (and every node inside it) and replacing each place that
    refers to a marked node covers the anchor and every alias at once.
    """
    secret: set[int] = set()
    _collect_secret_nodes(root, secret, set())
    if not secret:
        return 0
    return _replace_marked_nodes(root, secret, set())


def _collect_secret_nodes(node: yaml.Node, secret: set[int], seen: set[int]) -> None:
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and is_secret_name(str(key.value)):
                _mark_subtree(value, secret)
            else:
                _collect_secret_nodes(value, secret, seen)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _collect_secret_nodes(item, secret, seen)


def _mark_subtree(node: yaml.Node, secret: set[int]) -> None:
    if id(node) in secret:
        return
    secret.add(id(node))
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            _mark_subtree(key, secret)
            _mark_subtree(value, secret)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _mark_subtree(item, secret)


def _replace_marked_nodes(node: yaml.Node, secret: set[int], seen: set[int]) -> int:
    if id(node) in seen:
        return 0
    seen.add(id(node))
    count = 0
    if isinstance(node, yaml.MappingNode):
        pairs: list[tuple[yaml.Node, yaml.Node]] = []
        for key, value in node.value:
            if id(key) in secret:
                key, count = _redacted_node(), count + 1
            else:
                count += _replace_marked_nodes(key, secret, seen)
            if id(value) in secret:
                value, count = _redacted_node(), count + 1
            else:
                count += _replace_marked_nodes(value, secret, seen)
            pairs.append((key, value))
        node.value = pairs
    elif isinstance(node, yaml.SequenceNode):
        items: list[yaml.Node] = []
        for item in node.value:
            if id(item) in secret:
                item, count = _redacted_node(), count + 1
            else:
                count += _replace_marked_nodes(item, secret, seen)
            items.append(item)
        node.value = items
    return count


def _redacted_node() -> yaml.ScalarNode:
    return yaml.ScalarNode(_YAML_STR_TAG, REDACTED)
