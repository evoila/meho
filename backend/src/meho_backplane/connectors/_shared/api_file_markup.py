# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""XML and HTML for :mod:`.api_file_secrets`: parse, replace secrets, write back.

XML is parsed with :mod:`defusedxml` and written back with every
namespace declaration on the element that declared it, with the prefixes
as the file declared them, because WSDL and XSD files use the prefixes
inside attribute values (``type="xsd:string"``) and a prefix can mean
another namespace inside a child. HTML is parsed with :mod:`html.parser`
into a small tree and written back without comments; the parser
lowercases element and attribute names, so they are checked with the
rule for names whose case is lost. In both, a secret-named element loses
its content, a secret-named attribute loses its value, and an element
that a ``name``, ``id``, ``key`` or ``type`` attribute marks as secret
loses its content and every attribute value except the label that marks
it. A ``<script>`` block comes back only when its content is JSON.
"""

from __future__ import annotations

import html
import io
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Final

import defusedxml.ElementTree as DefusedET
from defusedxml import DefusedXmlException

from meho_backplane.broadcast.events import (
    is_secret_param_name,
    is_secret_param_name_lowercased,
)
from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    ApiFileFormatError,
    Scrubber,
    TextRule,
    dump_json,
)

__all__ = ["check_html", "check_xml"]

#: Attributes that name an element (``<input name="password">``). One
#: whose value is a secret name is a label and stays; the element's
#: content and every other attribute value are replaced.
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

#: One namespace declaration: ``(prefix, uri)``; the default namespace
#: has the empty prefix, ``xmlns=""`` has the empty uri too.
_Declaration = tuple[str, str]


# ---------------------------------------------------------------------------
# XML: parsed with defusedxml, written back with the prefixes as declared
# ---------------------------------------------------------------------------


def check_xml(text: str, path: str, text_rule: TextRule | None = None) -> tuple[str, int]:
    declared_on: dict[int, list[_Declaration]] = {}
    pending: list[_Declaration] = []
    try:
        events = DefusedET.iterparse(io.StringIO(text), events=("start-ns", "start"))
        for event, payload in events:
            if event == "start-ns":
                pending.append(payload)
            else:
                declared_on[id(payload)] = pending
                pending = []
        root = events.root
    except DefusedET.ParseError as exc:
        raise ApiFileFormatError(
            f"{path} is not well-formed XML ({exc}); the file is not returned"
        ) from None
    except DefusedXmlException as exc:
        raise ApiFileFormatError(
            f"{path} is XML that MEHO refuses to parse ({type(exc).__name__}); not returned"
        ) from None
    writer = _XmlWriter(declared_on, text_rule)
    out: list[str] = []
    writer.element(root, out, ())
    return "".join(out), writer.scrubber.count


def _split_tag(tag: str) -> tuple[str, str]:
    """Split ``{uri}local`` into ``(uri, local)``; a bare name has no uri."""
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return uri, local
    return "", tag


class _XmlWriter:
    """Write an element tree back as text, keeping the namespace declarations.

    ElementTree keeps a tag as ``{uri}local`` and forgets the prefix and
    where the namespace was declared. WSDL and XSD files use the prefixes
    inside attribute values (``type="xsd:string"``), and a child can bind
    a prefix to another namespace, so every declaration is written back
    on the element that declared it. A name is then qualified from the
    declarations in scope, innermost first.

    *declared_on* maps the id of each parsed element to the declarations
    the file made on it, in order.
    """

    def __init__(
        self, declared_on: dict[int, list[_Declaration]], text_rule: TextRule | None = None
    ) -> None:
        self.scrubber = Scrubber(text_rule=text_rule)
        self._declared_on = declared_on

    def element(self, el: Any, out: list[str], outer: tuple[list[_Declaration], ...]) -> None:
        """Write *el*, a node parsed by ``defusedxml``, and everything under it.

        The node is typed ``Any`` on purpose: naming the stdlib ``Element``
        type would import ``xml.etree``, which the security scan flags.
        *outer* holds the declarations of every enclosing element.
        """
        declarations = list(self._declared_on.get(id(el), ()))
        scopes = (*outer, declarations)
        uri, local = _split_tag(el.tag)
        secret = _element_is_secret(local, el.attrib.items(), _split_tag, is_secret_param_name)
        name = self._qualify(local, uri, scopes, attribute=False)
        attributes = [
            (
                self._qualify(attribute_local, attribute_uri, scopes, attribute=True),
                self._attribute_value(attribute_local, value, secret),
            )
            for key, value in el.attrib.items()
            for attribute_uri, attribute_local in (_split_tag(key),)
        ]
        head = [f"<{name}"]
        head.extend(
            f' xmlns:{prefix}="{_xml_attr(bound)}"' if prefix else f' xmlns="{_xml_attr(bound)}"'
            for prefix, bound in declarations
        )
        head.extend(f' {key}="{_xml_attr(value)}"' for key, value in attributes)
        out.append("".join(head))
        if secret and (el.text or len(el)):
            self.scrubber.count += 1
            out.append(f">{REDACTED}</{name}>")
        elif el.text or len(el):
            out.append(">")
            if el.text:
                out.append(_xml_text(self.scrubber.text(el.text)))
            for child in el:
                self.element(child, out, scopes)
            out.append(f"</{name}>")
        else:
            out.append("/>")
        if el.tail:
            out.append(_xml_text(self.scrubber.text(el.tail)))

    def _qualify(
        self, local: str, uri: str, scopes: tuple[list[_Declaration], ...], *, attribute: bool
    ) -> str:
        """The name to write for *local* in *uri*, from the declarations in scope.

        An attribute in a namespace always needs a prefix, never the
        default. A name without a namespace needs ``xmlns=""`` when a
        default namespace is in scope. When nothing in scope fits (the
        parser accepted the file, so this does not happen for its own
        names), a declaration is added to the innermost element.
        """
        if uri == _XML_NAMESPACE:
            return f"xml:{local}"
        if not uri:
            if not attribute and _bound_uri(scopes, ""):
                scopes[-1].append(("", ""))
            return local
        prefix = _prefix_in_scope(scopes, uri, attribute=attribute)
        if prefix is None:
            prefix = _fresh_prefix(scopes)
            scopes[-1].append((prefix, uri))
        return f"{prefix}:{local}" if prefix else local

    def _attribute_value(self, local: str, value: str, element_is_secret: bool) -> str:
        if is_secret_param_name(local) or (element_is_secret and not _is_label(local, value)):
            self.scrubber.count += 1
            return REDACTED
        return self.scrubber.text(value)


def _bound_uri(scopes: tuple[list[_Declaration], ...], prefix: str) -> str | None:
    """The uri *prefix* is bound to in the innermost scope that binds it."""
    for scope in reversed(scopes):
        for bound_prefix, uri in scope:
            if bound_prefix == prefix:
                return uri
    return None


def _prefix_in_scope(
    scopes: tuple[list[_Declaration], ...], uri: str, *, attribute: bool
) -> str | None:
    """A prefix bound to *uri* that no inner scope rebinds; ``None`` when there is none.

    An element whose namespace is the default in scope is written without
    a prefix, as a schema file usually writes it.
    """
    if not attribute and _bound_uri(scopes, "") == uri:
        return ""
    rebound: set[str] = set()
    for scope in reversed(scopes):
        for prefix, bound in scope:
            if prefix in rebound:
                continue
            rebound.add(prefix)
            if bound == uri and (prefix or not attribute):
                return prefix
    return None


def _fresh_prefix(scopes: tuple[list[_Declaration], ...]) -> str:
    number = 0
    while _bound_uri(scopes, f"ns{number}") is not None:
        number += 1
    return f"ns{number}"


def _element_is_secret(
    local: str,
    attributes: Iterable[tuple[str, str | None]],
    split: Callable[[str], tuple[str, str]],
    name_is_secret: Callable[[str], bool],
) -> bool:
    """True for a secret-named element, or one a naming attribute makes secret.

    *attributes* is every pair the parser saw, so a repeated attribute
    name cannot hide an earlier value. *name_is_secret* checks the
    element name, which an HTML parser lowercases; attribute values keep
    their case and go through the exact rule.
    """
    if name_is_secret(local):
        return True
    for key, value in attributes:
        if split(key)[1] in _NAMING_ATTRIBUTES and value and is_secret_param_name(value):
            return True
    return False


def _is_label(local: str, value: str | None) -> bool:
    """True for a naming attribute whose value is a secret name.

    Such an attribute is what marks its element as secret
    (``name="password"``); it stays so the reader sees which value was
    replaced. Every other attribute of the element is replaced, also
    another naming attribute (``id="abc"``): its value can be anything.
    """
    return local in _NAMING_ATTRIBUTES and bool(value) and is_secret_param_name(value or "")


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


def check_html(text: str, _path: str, text_rule: TextRule | None = None) -> tuple[str, int]:
    parser = _HtmlTree()
    parser.feed(text)
    parser.close()
    scrubber = Scrubber(text_rule=text_rule)
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
    secret = _element_is_secret(
        node.tag.rpartition(":")[2], node.attrs, _html_name, is_secret_param_name_lowercased
    )
    head = [f"<{node.tag}"]
    for key, value in node.attrs:
        if value is None:
            head.append(f" {key}")
            continue
        if is_secret_param_name_lowercased(key) or (secret and not _is_label(key, value)):
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


def _html_name(key: str) -> tuple[str, str]:
    """An HTML attribute name has no namespace."""
    return "", key


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
