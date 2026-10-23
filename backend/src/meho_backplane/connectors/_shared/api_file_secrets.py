# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check an API description file by parsing it; replace every secret value.

An API description file is a file an appliance serves to describe its
own API: a Swagger or OpenAPI file (JSON or YAML), a WSDL or XSD file
(XML) or a documentation page (HTML). Such a file can carry a clear-text
secret: an example password in a request sample, a licence key in a
list, a token in a form field, a private key in a text block.

A text scan for such values is a race against the next trick: a value on
the next line, a YAML alias, an escaped key, a quoted ``>`` inside an
HTML attribute. This helper reads the file with a real parser for its
format instead, so a secret is the value under a secret-named key,
wherever it hides and however it is written. The checked copy is written
back from the parsed file, so comments, repeated keys and anything else
the parser skips never reach the caller.

What the helper does, in order:

1. Picks the parser from the content type: JSON (:mod:`json`), YAML
   (PyYAML, safe loader), XML (:mod:`defusedxml`) or HTML
   (:mod:`html.parser`). When the content type is ``text/plain`` or
   missing, the file ending decides (``.json``, ``.yaml``, ``.yml``,
   ``.xml``, ``.wsdl``, ``.xsd``, ``.html``, ``.htm``). YAML is never
   tried on other text: almost any text is valid YAML.
2. Refuses a file that does not parse, or that has no known format (for
   example JavaScript or CSS), with :exc:`ApiFileFormatError`. The
   message names the reason, never the text.
3. Walks the parsed file and replaces every secret value with
   :data:`REDACTED`, the placeholder the governed Vault read returns. A
   name is secret-named by MEHO's one rule for that,
   :func:`~meho_backplane.broadcast.events.is_secret_param_name`.

   * JSON and YAML: the value of every secret-named key, at any depth,
     also inside lists. The whole value goes, whatever it is: text,
     number, list or object. A YAML alias (``*name``) that points to a
     secret value is replaced where the anchor is written too.
   * XML and HTML: the whole content of a secret-named element (text and
     child elements), and the value of a secret-named attribute. An
     element whose ``name``, ``id``, ``key`` or ``type`` attribute is a
     secret name (``<input type="password" value="...">``) loses its
     content and the values of its other attributes.
   * HTML ``<script>`` blocks: JSON content is checked like a JSON file.
     Any other script content becomes the placeholder; the helper cannot
     parse JavaScript, so it does not return it.
   * A text value that is itself a JSON object or list (a sample request
     in a description, a ``<pre>`` block) is parsed and checked the same
     way.
   * A text value that holds a private key in PEM form, under any name.

4. Writes the checked copy in the same format and counts the replaced
   values.

:func:`scrub_query_string` does the same for the query of a redirect
path: the names are decoded first (``license%4Bey`` reads as
``licenseKey``), and a secret-named value is replaced.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode

import yaml

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors._shared.api_file_markup import check_html, check_xml
from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    ApiFileFormatError,
    Scrubber,
    dump_json,
)

__all__ = [
    "REDACTED",
    "ApiFileFormatError",
    "CheckedApiFile",
    "check_api_file",
    "detect_format",
    "scrub_query_string",
]

#: Content types per format. A type that ends in ``+json``, ``+yaml`` or
#: ``+xml`` counts as that format too.
_MEDIA_TYPES: Final[dict[str, str]] = {
    "application/json": "json",
    "text/json": "json",
    "application/yaml": "yaml",
    "application/x-yaml": "yaml",
    "text/yaml": "yaml",
    "text/x-yaml": "yaml",
    "text/vnd.yaml": "yaml",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/html": "html",
}
_MEDIA_SUFFIXES: Final[dict[str, str]] = {"+json": "json", "+yaml": "yaml", "+xml": "xml"}
#: Content types that say nothing about the format; the file ending decides.
_MEDIA_FROM_ENDING: Final[frozenset[str]] = frozenset({"", "text/plain"})
_ENDINGS: Final[dict[str, str]] = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".wsdl": "xml",
    ".xsd": "xml",
    ".html": "html",
    ".htm": "html",
}

_YAML_STR_TAG: Final[str] = "tag:yaml.org,2002:str"
_YAML_LOADER: Final[type[yaml.SafeLoader]] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YAML_DUMPER: Final[type[yaml.SafeDumper]] = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


@dataclass(frozen=True, slots=True)
class CheckedApiFile:
    """The checked copy of a file and how many values were replaced in it."""

    text: str
    secrets_replaced: int


def detect_format(content_type: str | None, path: str) -> str | None:
    """Return ``json``, ``yaml``, ``xml`` or ``html`` for the file, or ``None``.

    The content type decides. When it is ``text/plain`` or missing, the
    ending of *path* (without its query) decides.
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media in _MEDIA_TYPES:
        return _MEDIA_TYPES[media]
    for suffix, fmt in _MEDIA_SUFFIXES.items():
        if media.endswith(suffix):
            return fmt
    if media not in _MEDIA_FROM_ENDING:
        return None
    name = path.partition("?")[0].lower()
    for ending, fmt in _ENDINGS.items():
        if name.endswith(ending):
            return fmt
    return None


def check_api_file(text: str, *, content_type: str | None, path: str) -> CheckedApiFile:
    """Parse *text*, replace every secret value, write the copy back.

    Raises :exc:`ApiFileFormatError` when the format is unknown or the
    file does not parse. An empty file comes back empty.
    """
    if not text:
        return CheckedApiFile("", 0)
    fmt = detect_format(content_type, path)
    if fmt is None:
        raise ApiFileFormatError(
            f"{path} has no known format: content type {content_type!r} is not JSON, YAML, "
            f"XML or HTML and the path ends in none of {sorted(_ENDINGS)}; the file is not "
            "returned"
        )
    try:
        copy, count = _CHECKERS[fmt](text, path)
    except RecursionError:
        raise ApiFileFormatError(f"{path} is nested too deeply to check; not returned") from None
    return CheckedApiFile(copy, count)


def scrub_query_string(path: str) -> tuple[str, int]:
    """Replace secret-named values in the query of *path*; return it and the count.

    The names are decoded before the check, so an encoded name cannot hide
    a secret-named key. A path whose query holds no secret comes back as
    written.
    """
    base, separator, query = path.partition("?")
    if not separator:
        return path, 0
    count = 0
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if is_secret_param_name(key):
            pairs.append((key, REDACTED))
            count += 1
        else:
            pairs.append((key, value))
    if not count:
        return path, 0
    return f"{base}?{urlencode(pairs, safe='[]:')}", count


# ---------------------------------------------------------------------------
# The walk over parsed values (JSON, YAML, and text that holds JSON)
# ---------------------------------------------------------------------------


def _check_json(text: str, path: str) -> tuple[str, int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApiFileFormatError(
            f"{path} is not well-formed JSON ({exc.msg} at line {exc.lineno}, column "
            f"{exc.colno}); the file is not returned"
        ) from None
    scrubber = Scrubber()
    copy = scrubber.tree(data, {})
    return dump_json(copy), scrubber.count


# ---------------------------------------------------------------------------
# YAML: aliases are handled on the node graph, before the values exist
# ---------------------------------------------------------------------------


def _check_yaml(text: str, path: str) -> tuple[str, int]:
    loader = _YAML_LOADER(text)
    try:
        try:
            root = loader.get_single_node()
            if root is None:
                return "", 0
            count = _redact_yaml_nodes(root)
            data = loader.construct_document(root)
        except RecursionError:
            raise
        except Exception as exc:
            raise ApiFileFormatError(
                f"{path} is not well-formed YAML ({_yaml_reason(exc)}); the file is not returned"
            ) from None
    finally:
        loader.dispose()
    scrubber = Scrubber(count)
    copy = scrubber.tree(data, {})
    try:
        text_out = yaml.dump(copy, Dumper=_YAML_DUMPER, sort_keys=False, allow_unicode=True)
    except yaml.YAMLError as exc:
        raise ApiFileFormatError(
            f"{path} cannot be written back as YAML ({type(exc).__name__}); not returned"
        ) from None
    return text_out, scrubber.count


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
            if isinstance(key, yaml.ScalarNode) and is_secret_param_name(str(key.value)):
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


_CHECKERS: Final[dict[str, Callable[[str, str], tuple[str, int]]]] = {
    "json": _check_json,
    "yaml": _check_yaml,
    "xml": check_xml,
    "html": check_html,
}
