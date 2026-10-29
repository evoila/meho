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
it. A ``<script>`` block comes back only when its content is JSON. Every
JSON object in a text or attribute value is checked like a JSON file and
written back from the parsed copy (:meth:`Scrubber.text`).

An HTML ``<pre>`` or ``<code>`` block is an example, and the check of an
example fails closed (:func:`_html_example_block`). The block comes back
only when the check could read all of it: one JSON or YAML document,
maybe after one request line (``POST /sessions``), parsed whole and
checked key by key, or plain text. It then comes back written from the
parsed copy, with the secret values replaced. Any other block is
withheld: the placeholder stands in its place, counted once, and the
reason is kept, never the text. A block is withheld when it does not
parse, holds more than one document, names a key twice at any depth,
holds text that is not valid Unicode, has a child element that is
secret or a script, or is plain text that names a secret next to a
value.
"""

from __future__ import annotations

import html
import io
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Final

import defusedxml.ElementTree as DefusedET
from defusedxml import DefusedXmlException

from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    REPEATED_KEY,
    SECRET_CHILD_ELEMENT,
    SECRET_NAME_IN_TEXT,
    TOO_DEEP,
    ApiFileFormatError,
    RepeatedKeyError,
    Scrubber,
    TextRule,
    WithheldError,
    dump_json,
    is_secret_name,
    is_secret_name_lowercased,
    load_json,
)
from meho_backplane.connectors._shared.api_file_yaml import check_yaml_example

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

#: HTML blocks that hold an example. Their check fails closed
#: (:func:`_html_example_block`). A ``<span>`` is not one: it is an inline
#: element of ordinary page text, which the element check covers.
_HTML_DOCUMENT_BLOCKS: Final[frozenset[str]] = frozenset({"pre", "code"})

#: A request line before the body of an example: a method and a path,
#: maybe the HTTP version, alone on the first line. A path with a query
#: (``?``) is no request line, so the whole text must then be one
#: document.
_REQUEST_LINE: Final[re.Pattern[str]] = re.compile(
    r"[ \t]*(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[ \t]+/[^\s?#]*"
    r"(?:[ \t]+HTTP/\d(?:\.\d)?)?[ \t]*(?:\r?\n|\Z)"
)

#: One namespace declaration: ``(prefix, uri)``; the default namespace
#: has the empty prefix, ``xmlns=""`` has the empty uri too.
_Declaration = tuple[str, str]


# ---------------------------------------------------------------------------
# XML: parsed with defusedxml, written back with the prefixes as declared
# ---------------------------------------------------------------------------


def check_xml(text: str, path: str, text_rule: TextRule | None = None) -> tuple[str, Scrubber]:
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
    return "".join(out), writer.scrubber


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
        self.scrubber = Scrubber(text_rule=text_rule, strict=True)
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
        secret = _element_is_secret(local, el.attrib.items(), _split_tag, is_secret_name)
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
        if is_secret_name(local) or (element_is_secret and not _is_label(local, value)):
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
        if split(key)[1] in _NAMING_ATTRIBUTES and value and is_secret_name(value):
            return True
    return False


def _is_label(local: str, value: str | None) -> bool:
    """True for a naming attribute whose value is a secret name.

    Such an attribute is what marks its element as secret
    (``name="password"``); it stays so the reader sees which value was
    replaced. Every other attribute of the element is replaced, also
    another naming attribute (``id="abc"``): its value can be anything.
    """
    return local in _NAMING_ATTRIBUTES and bool(value) and is_secret_name(value or "")


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


def check_html(text: str, _path: str, text_rule: TextRule | None = None) -> tuple[str, Scrubber]:
    parser = _HtmlTree()
    parser.feed(text)
    parser.close()
    scrubber = Scrubber(text_rule=text_rule, strict=True)
    out: list[str] = []
    _html_children(parser.root, out, scrubber)
    return "".join(out), scrubber


def _html_children(node: _HtmlNode, out: list[str], scrubber: Scrubber) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(html.escape(scrubber.text(child), quote=False))
        elif child.tag == "!":
            out.append(f"<!{child.children[0]}>")
        else:
            _html_element(child, out, scrubber)


def _html_element(node: _HtmlNode, out: list[str], scrubber: Scrubber) -> None:
    secret = _html_element_is_secret(node)
    head = [f"<{node.tag}"]
    for key, value in node.attrs:
        if value is None:
            head.append(f" {key}")
            continue
        if is_secret_name_lowercased(key) or (secret and not _is_label(key, value)):
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
    elif node.tag in _HTML_DOCUMENT_BLOCKS:
        out.append(_html_example_block(node, scrubber))
    elif node.tag == "style":
        out.extend(scrubber.text(c) for c in node.children if isinstance(c, str))
    else:
        _html_children(node, out, scrubber)
    out.append(f"</{node.tag}>")


def _html_example_block(node: _HtmlNode, scrubber: Scrubber) -> str:
    """A ``<pre>`` or ``<code>`` block: the checked example, or the placeholder.

    The block fails closed. Its text, joined over its child elements as
    the page wrote it (syntax highlighting splits an example into
    ``<span>`` elements), is one example, checked by :func:`_example`.
    The block comes back only when that check read all of the text; it
    then comes back as the checked text, without the child elements.
    Any other block is withheld: the placeholder, counted once, with the
    reason kept on *scrubber*, never the text. A block is withheld
    whole, without its text, when a child element is secret (its name,
    a naming attribute or a secret-named attribute says so) or is a
    script: its text belongs to that element, not to the example. A
    block with no text keeps its child elements, each with its own
    check.
    """
    if _hides_a_secret(node):
        return scrubber.withhold(SECRET_CHILD_ELEMENT)
    text = "".join(_html_text(node))
    if not text.strip():
        out: list[str] = []
        _html_children(node, out, scrubber)
        return "".join(out)
    probe = scrubber.fresh()
    try:
        checked = _example(text, probe)
    except WithheldError as exc:
        return scrubber.withhold(exc.reason)
    scrubber.absorb(probe)
    return html.escape(checked, quote=False)


def _hides_a_secret(node: _HtmlNode) -> bool:
    """True when an element under *node* is secret, has a secret-named attribute, or is a script."""
    for child in node.children:
        if isinstance(child, str) or child.tag == "!":
            continue
        if (
            child.tag == "script"
            or _html_element_is_secret(child)
            or any(is_secret_name_lowercased(key) for key, _ in child.attrs)
            or _hides_a_secret(child)
        ):
            return True
    return False


def _example(text: str, scrubber: Scrubber) -> str:
    """*text*, one example, checked whole; :exc:`WithheldError` when it cannot be.

    An example is one JSON or YAML document, maybe after one request line
    (:data:`_REQUEST_LINE`), or plain text:

    * JSON is parsed whole and strict, so a key named twice in one object,
      at any depth, withholds the example. An object or a list comes back
      written from the parsed copy, every secret value replaced.
    * Text that is not JSON is read as one YAML document
      (:func:`check_yaml_example`). A mapping or a list comes back from
      the parsed copy too. A second document, a parse error or a key
      named twice withholds the example.
    * A text that is one scalar (a shell command, a path, a word) or no
      document at all is plain text. It has no names, so the check runs
      the rules for any text value (:meth:`Scrubber.text`): every JSON
      document in it is checked, a start that looks like JSON must
      parse, and a secret name next to a value (``sessionId:x``)
      withholds the text. When YAML reads the scalar as other words
      than the text shows (quotes, escapes), the value is checked too,
      and anything that check would replace withholds the example: a
      YAML escape (``"pass\\x77ord: ..."``) cannot hide a name.

    The request line stays as written, and so does the white space around
    the document.
    """
    match = _REQUEST_LINE.match(text)
    head = match.group(0) if match else ""
    body = text[len(head) :]
    core = body.strip()
    if not core:
        return scrubber.text(text)
    lead = body[: len(body) - len(body.lstrip())]
    trail = body[len(body.rstrip()) :]
    value: Any
    try:
        value = load_json(core)
    except RepeatedKeyError:
        raise WithheldError(REPEATED_KEY) from None
    except RecursionError:
        raise WithheldError(TOO_DEEP) from None
    except ValueError:
        copy, value = check_yaml_example(core, scrubber)
        if copy is not None:
            return head + lead + copy.rstrip("\n") + trail
    else:
        if isinstance(value, (dict, list)):
            return head + lead + dump_json(scrubber.tree(value, {})) + trail
    if value is not None and str(value).split() != core.split():
        check = scrubber.fresh()
        check.text(str(value))
        if check.count:
            raise WithheldError(check.withheld[0] if check.withheld else SECRET_NAME_IN_TEXT)
    return scrubber.text(text)


def _html_text(node: _HtmlNode) -> Iterable[str]:
    """Every text value under *node*, in order, as the page wrote it, without the markup."""
    for child in node.children:
        if isinstance(child, str):
            yield child
        elif child.tag != "!":
            yield from _html_text(child)


def _html_element_is_secret(node: _HtmlNode) -> bool:
    return _element_is_secret(
        node.tag.rpartition(":")[2], node.attrs, _html_name, is_secret_name_lowercased
    )


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
