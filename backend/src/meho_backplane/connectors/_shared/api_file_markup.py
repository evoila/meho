# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""XML and HTML for :mod:`.api_file_secrets`: parse, replace secrets, write back.

XML is parsed with :mod:`defusedxml` and written back with the namespace
prefixes as the file declared them, because WSDL and XSD files use the
prefixes inside attribute values (``type="xsd:string"``). HTML is parsed
with :mod:`html.parser` into a small tree and written back without
comments. In both, a secret-named element loses its content, a
secret-named attribute loses its value, and an element that a ``name``,
``id``, ``key`` or ``type`` attribute marks as secret loses its content
and the values of its other attributes. A ``<script>`` block comes back
only when its content is JSON.
"""

from __future__ import annotations

import html
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as DefusedET
from defusedxml import DefusedXmlException

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    ApiFileFormatError,
    Scrubber,
    dump_json,
)

__all__ = ["check_html", "check_xml"]

#: Attributes that name an element (``<input name="password">``). Their
#: values are labels and stay; the element's content and other attributes
#: are replaced when one of them is a secret name.
_NAMING_ATTRIBUTES: Final[frozenset[str]] = frozenset({"name", "id", "key", "type"})

#: HTML elements that never have content or a closing tag.
_HTML_VOID: Final[frozenset[str]] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_XML_NAMESPACE: Final[str] = "http://www.w3.org/XML/1998/namespace"


# ---------------------------------------------------------------------------
# XML: parsed with defusedxml, written back with the prefixes as declared
# ---------------------------------------------------------------------------


def check_xml(text: str, path: str) -> tuple[str, int]:
    try:
        events = DefusedET.iterparse(io.StringIO(text), events=("start-ns",))
        declared = [(prefix, uri) for _event, (prefix, uri) in events]
        root = events.root
    except DefusedET.ParseError as exc:
        raise ApiFileFormatError(
            f"{path} is not well-formed XML ({exc}); the file is not returned"
        ) from None
    except DefusedXmlException as exc:
        raise ApiFileFormatError(
            f"{path} is XML that MEHO refuses to parse ({type(exc).__name__}); not returned"
        ) from None
    writer = _XmlWriter(declared)
    out: list[str] = []
    writer.element(root, out, "", first=True)
    return "".join(out), writer.scrubber.count


def _split_tag(tag: str) -> tuple[str, str]:
    """Split ``{uri}local`` into ``(uri, local)``; a bare name has no uri."""
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return uri, local
    return "", tag


class _XmlWriter:
    """Write an element tree back as text, keeping the namespace prefixes.

    ElementTree keeps a tag as ``{uri}local`` and forgets the prefix. WSDL
    and XSD files use the prefixes inside attribute values (``type=
    "xsd:string"``), so they must come back as declared. Every prefixed
    declaration of the file goes on the root element; a default namespace
    is declared where it starts, as in the file.
    """

    def __init__(self, declared: list[tuple[str, str]]) -> None:
        self.scrubber = Scrubber()
        self._prefix_of_uri: dict[str, str] = {_XML_NAMESPACE: "xml"}
        self._declarations: list[tuple[str, str]] = []
        self._used = {"xml", "xmlns"}
        for prefix, uri in declared:
            if prefix and prefix in self._used:
                prefix = self._fresh_prefix()
            if prefix:
                self._used.add(prefix)
                self._declarations.append((prefix, uri))
            self._prefix_of_uri.setdefault(uri, prefix)

    def _fresh_prefix(self) -> str:
        number = 0
        while f"ns{number}" in self._used:
            number += 1
        return f"ns{number}"

    def _attribute_prefix(self, uri: str) -> str:
        """An attribute in a namespace always needs a prefix, never the default."""
        prefix = self._prefix_of_uri.get(uri, "")
        if prefix:
            return prefix
        prefix = self._fresh_prefix()
        self._used.add(prefix)
        self._declarations.append((prefix, uri))
        self._prefix_of_uri[uri] = prefix
        return prefix

    def element(self, el: Element, out: list[str], default_uri: str, *, first: bool) -> None:
        uri, local = _split_tag(el.tag)
        prefix = self._prefix_of_uri.get(uri, "")
        if prefix:
            name = f"{prefix}:{local}"
            child_default = default_uri
            declare_default = None
        else:
            name = local
            child_default = uri
            declare_default = uri if uri != default_uri else None
        attributes = self._attributes(el, _element_is_secret(local, el.attrib, _split_tag))
        head = [f"<{name}"]
        if first:
            head.extend(f' xmlns:{p}="{_xml_attr(u)}"' for p, u in self._declarations)
        if declare_default is not None:
            head.append(f' xmlns="{_xml_attr(declare_default)}"')
        head.extend(f' {key}="{_xml_attr(value)}"' for key, value in attributes)
        out.append("".join(head))
        if _element_is_secret(local, el.attrib, _split_tag) and (el.text or len(el)):
            self.scrubber.count += 1
            out.append(f">{REDACTED}</{name}>")
        elif el.text or len(el):
            out.append(">")
            if el.text:
                out.append(_xml_text(self.scrubber.text(el.text)))
            for child in el:
                self.element(child, out, child_default, first=False)
            out.append(f"</{name}>")
        else:
            out.append("/>")
        if el.tail:
            out.append(_xml_text(self.scrubber.text(el.tail)))

    def _attributes(self, el: Element, element_is_secret: bool) -> list[tuple[str, str]]:
        written: list[tuple[str, str]] = []
        for key, value in el.attrib.items():
            uri, local = _split_tag(key)
            name = f"{self._attribute_prefix(uri)}:{local}" if uri else local
            written.append((name, self._attribute_value(local, value, element_is_secret)))
        return written

    def _attribute_value(self, local: str, value: str, element_is_secret: bool) -> str:
        if is_secret_param_name(local) or (element_is_secret and local not in _NAMING_ATTRIBUTES):
            self.scrubber.count += 1
            return REDACTED
        return self.scrubber.text(value)


def _element_is_secret(
    local: str, attrib: Mapping[str, str | None], split: Callable[[str], tuple[str, str]]
) -> bool:
    """True for a secret-named element, or one a naming attribute makes secret."""
    if is_secret_param_name(local):
        return True
    for key, value in attrib.items():
        if split(key)[1] in _NAMING_ATTRIBUTES and value and is_secret_param_name(value):
            return True
    return False


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(value: str) -> str:
    return (
        _xml_text(value)
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
        .replace("\t", "&#9;")
    )


# ---------------------------------------------------------------------------
# HTML: a small tree from html.parser, written back without comments
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _HtmlNode:
    """One element (``tag`` and ``attrs``), or a declaration (``tag`` is ``!``)."""

    tag: str
    attrs: list[tuple[str, str | None]]
    children: list[_HtmlNode | str]


class _HtmlTree(HTMLParser):
    """Build a tree of :class:`_HtmlNode`; comments and processing instructions are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("", [], [])
        self._open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(tag, attrs, [])
        self._open[-1].children.append(node)
        if tag not in _HTML_VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open[-1].children.append(_HtmlNode(tag, attrs, []))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        children = self._open[-1].children
        if children and isinstance(children[-1], str):
            children[-1] += data
        else:
            children.append(data)

    def handle_decl(self, decl: str) -> None:
        self._open[-1].children.append(_HtmlNode("!", [], [decl]))


def check_html(text: str, _path: str) -> tuple[str, int]:
    parser = _HtmlTree()
    parser.feed(text)
    parser.close()
    scrubber = Scrubber()
    out: list[str] = []
    _html_children(parser.root, out, scrubber)
    return "".join(out), scrubber.count


def _html_children(node: _HtmlNode, out: list[str], scrubber: Scrubber) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(html.escape(scrubber.text(child), quote=False))
        elif child.tag == "!":
            out.append(f"<!{child.children[0]}>")
        else:
            _html_element(child, out, scrubber)


def _html_element(node: _HtmlNode, out: list[str], scrubber: Scrubber) -> None:
    attrib = dict(node.attrs)
    secret = _element_is_secret(node.tag.rpartition(":")[2], attrib, lambda key: ("", key))
    head = [f"<{node.tag}"]
    for key, value in node.attrs:
        if value is None:
            head.append(f" {key}")
            continue
        if is_secret_param_name(key) or (secret and key not in _NAMING_ATTRIBUTES):
            scrubber.count += 1
            value = REDACTED
        else:
            value = scrubber.text(value)
        head.append(f' {key}="{html.escape(value, quote=True)}"')
    head.append(">")
    out.append("".join(head))
    if node.tag in _HTML_VOID:
        return
    if secret and node.children:
        scrubber.count += 1
        out.append(REDACTED)
    elif node.tag == "script":
        out.append(_html_script(node, scrubber))
    elif node.tag == "style":
        out.extend(scrubber.text(c) for c in node.children if isinstance(c, str))
    else:
        _html_children(node, out, scrubber)
    out.append(f"</{node.tag}>")


def _html_script(node: _HtmlNode, scrubber: Scrubber) -> str:
    """JSON script content is checked like a JSON file; any other content is replaced."""
    content = "".join(c for c in node.children if isinstance(c, str))
    if not content.strip():
        return content
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = None
    if not isinstance(parsed, (dict, list)):
        scrubber.count += 1
        return REDACTED
    return dump_json(scrubber.tree(parsed, {}))
