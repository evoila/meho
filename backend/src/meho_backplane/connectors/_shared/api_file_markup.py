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
JSON object in a text or attribute value (the body after a request line
in a ``<pre>`` block, for example; also a list that holds an object) is
checked like a JSON file and written back from the parsed copy
(:meth:`Scrubber.text`). A ``<pre>`` or ``<code>`` block that holds text
alone and is a YAML mapping or list (not JSON) is an example in YAML: it
is checked like a YAML file and written back from the parsed copy, so a
comment or a repeated key does not come back. A ``<pre>`` or ``<code>``
block with child elements (syntax highlighting with
``<span>``) fails closed: it comes back as it was only when every child
passes its own check unchanged and the text joined over the children is
one JSON or YAML object or list that the structured check leaves
unchanged. Text that names a key twice in one object, at any depth, or
YAML text that holds a comment, is no such document: the parser would
keep only part of it, and the block would come back with text the check
never saw. A text value inside the document that is itself JSON with a
key named twice counts as a replacement for the same reason. Any other
such block is withheld: the placeholder stands in its place, counted
once.
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
import yaml
from defusedxml import DefusedXmlException

from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    ApiFileFormatError,
    RepeatedKeyError,
    Scrubber,
    TextRule,
    dump_json,
    is_secret_name,
    is_secret_name_lowercased,
    json_documents,
    load_json,
)
from meho_backplane.connectors._shared.api_file_yaml import check_yaml_text

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

#: HTML blocks that hold a whole example: a YAML document in one, as text
#: alone, is checked like a YAML file; one split by child elements
#: (syntax highlighting) fails closed. A ``<span>`` is neither: it is an
#: inline element of ordinary page text, which the element check covers
#: (every JSON document in its text is checked like in any other text).
_HTML_DOCUMENT_BLOCKS: Final[frozenset[str]] = frozenset({"pre", "code"})

_YAML_LOADER: Final[type[yaml.SafeLoader]] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

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
    elif node.tag in _HTML_DOCUMENT_BLOCKS and _has_elements(node):
        out.append(_html_block(node, scrubber))
    elif node.tag in _HTML_DOCUMENT_BLOCKS:
        out.append(_html_example(node, scrubber))
    elif node.tag == "style":
        out.extend(scrubber.text(c) for c in node.children if isinstance(c, str))
    else:
        _html_children(node, out, scrubber)
    out.append(f"</{node.tag}>")


def _has_elements(node: _HtmlNode) -> bool:
    return any(not isinstance(child, str) for child in node.children)


def _html_block(node: _HtmlNode, scrubber: Scrubber) -> str:
    """A ``<pre>`` or ``<code>`` block with child elements: as it was, or the placeholder.

    The block fails closed. Two checks run on it, and it comes back as it
    was only when neither would replace anything: every child element is
    checked as it would be on its own (a secret-marked child, a
    secret-named attribute, a script that is not JSON), and the block's
    text, joined over the children as the page wrote it, is checked as
    one JSON or YAML object or list. Text that is no such document, or a
    check that would replace anything, withholds the whole block: the
    placeholder in its place, counted once. The checks are never
    combined into a rebuilt block: a child element can split a secret
    name (``{"<span>pass</span>word": ...}``) or stand inside a key, so
    that text with the children replaced is another document than the
    one the page shows. The block comes back as the page wrote it, so
    the parsed copy must hold all of its text: a key named twice in one
    object (the parser keeps one value and drops the other) or a YAML
    comment makes the text no document, see :func:`_parse_document`.
    The same holds one level down: a text value in the document that is
    itself JSON with a key named twice is withheld and counted by
    :meth:`Scrubber.text`, so the probe's count is never zero while the
    text holds a value the parser dropped. A block with no text has only
    the element check.
    """
    probe = scrubber.fresh()
    checked: list[str] = []
    _html_children(node, checked, probe)
    text = "".join(_html_text(node))
    parsed = _parse_document(text)
    if parsed is not None:
        probe.tree(parsed, {})
    if probe.count or (parsed is None and text.strip()):
        scrubber.count += 1
        return REDACTED
    return "".join(checked)


def _parse_document(text: str) -> dict[str, Any] | list[Any] | None:
    """*text* as one JSON or YAML object or list; ``None`` when it is not one.

    JSON is tried first, then YAML with the safe loader. A YAML scalar is
    not a document: almost any text is one. Text the loader refuses or
    cannot build, for any reason, is not a document either. Neither is
    text that names a key twice in one object, at any depth, nor YAML
    text with a ``#`` in it: the parser keeps only part of such text (one
    of the two values; the text without the comment), and a check of the
    parsed copy says nothing about the rest. The caller returns the text
    as the page wrote it, so all of it must be in the parsed copy. A
    text value inside the document that is itself JSON gets the same
    rule from :meth:`Scrubber.text`, which withholds and counts it.
    """
    try:
        parsed: Any = load_json(text)
    except RepeatedKeyError:
        return None
    except ValueError:
        parsed = _load_yaml(text)
    except RecursionError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _load_yaml(text: str) -> Any:
    if "#" in text:
        return None
    loader = _YAML_LOADER(text)
    try:
        node = loader.get_single_node()
        if node is None or _yaml_repeats_a_key(loader, node, set()):
            return None
        return loader.construct_document(node)
    except Exception:  # any failure to read the text means it is no document
        return None
    finally:
        loader.dispose()


def _yaml_repeats_a_key(loader: Any, node: Any, seen: set[int]) -> bool:
    """True when a mapping under *node* names a key twice, or has a key that is no scalar.

    The loader builds a mapping by assigning key after key, so a repeated
    key keeps the last value only. Keys are compared as the loader
    builds them (``1`` and ``"1"`` differ, ``yes`` and ``true`` do not).
    A key that is a mapping or a list, and a merge key (``<<``), are not
    accepted: the block fails closed on them. An alias stands for a node
    already seen, which is checked once.
    """
    if id(node) in seen:
        return False
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        return _yaml_mapping_repeats_a_key(loader, node, seen)
    items = node.value if isinstance(node, yaml.SequenceNode) else ()
    return any(_yaml_repeats_a_key(loader, item, seen) for item in items)


def _yaml_mapping_repeats_a_key(loader: Any, node: Any, seen: set[int]) -> bool:
    keys: set[Any] = set()
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            return True
        key = loader.construct_object(key_node)
        if key in keys or _yaml_repeats_a_key(loader, value_node, seen):
            return True
        keys.add(key)
    return False


def _html_example(node: _HtmlNode, scrubber: Scrubber) -> str:
    """A ``<pre>`` or ``<code>`` block that holds text alone, checked and escaped.

    A page writes an example as one text, often with the request line
    before the body (``POST /sessions`` and then the JSON). Every JSON
    document in the text is checked like a JSON file and written back
    from the parsed copy, and the text around them goes through the text
    rule (:meth:`Scrubber.text`). A block whose text is not JSON but a
    YAML mapping or list (``sessionId: ...``) is checked like a YAML file
    and written back from the parsed copy (:func:`check_yaml_text`): a
    comment or a repeated key is not in the copy, so a secret the parser
    dropped does not come back. YAML is tried before the JSON documents
    in the text, so a YAML example with a flow mapping in it is checked
    whole. A block the check cannot finish (JSON that names a key twice,
    brackets that open over and over, a copy the dumper cannot write)
    comes back as the placeholder. Any other text (a request line, a
    shell command, prose) goes through the text rule.
    """
    text = "".join(_html_text(node))
    if json_documents(text) is None:
        scrubber.count += 1
        return REDACTED
    if not _is_json(text):
        probe = scrubber.fresh()
        try:
            document = check_yaml_text(text, probe)
        except yaml.YAMLError:
            scrubber.count += 1
            return REDACTED
        if document is not None:
            scrubber.count += probe.count
            if not text.endswith("\n"):
                document = document.rstrip("\n")
            return html.escape(document, quote=False)
    return html.escape(scrubber.text(text), quote=False)


def _is_json(text: str) -> bool:
    """True when *text* is one JSON value, whole (surrounding whitespace aside)."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        load_json(stripped)
    except ValueError:
        return False
    return True


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
