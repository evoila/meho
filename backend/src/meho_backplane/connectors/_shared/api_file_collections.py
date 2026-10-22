# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The scalars inside an object or list that a secret-named key holds.

Part of the whole-file secret scan in
:mod:`~meho_backplane.connectors._shared.api_file_secrets`. A key such as
``credentials`` or ``licenseKey`` can hold a collection instead of one
value: ``{"credentials": {"value": "..."}}``, ``licenseKey: [...]``, a
mapping on the lines below a YAML key. Every scalar inside counts, at
any depth, except under the words a schema uses: ``"password": {"type":
"string", "format": "password"}`` and a path item named
``/reset-password`` describe an API, they hold no secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from meho_backplane.connectors._shared.api_file_text import (
    FLOW_STOP,
    KEY_RE,
    SecretValue,
    decode_key,
    deeper_lines_end,
    end_of_line,
    is_comment,
    is_secret_name,
    is_stub,
    next_content_line,
    quoted_end,
    resolve_value,
    scalar_end,
)

__all__ = ["collection_hits"]

#: A bare number: never a value inside a collection, as in the broadcast
#: scrub, where a bare scalar under a secret-named key is not secret material.
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

#: The placeholder the scan writes, whole: it opens with ``[`` but is
#: not a list. Nothing may follow inside its brackets.
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\[REDACTED[^\]\s]*\](?=[\s,}\]]|$)")

#: Keys whose value describes an API instead of holding data, lower-cased:
#: the fixed fields of OpenAPI 3.1 (schema, parameter, security scheme,
#: OAuth flow, discriminator, XML, example and external documentation
#: objects), of Swagger 2 and of JSON Schema 2020-12. Inside a collection
#: under a secret-named key, the value of such a key, scalar or subtree,
#: is not a secret. ``value`` is not here: an example's ``value`` is data.
_SCHEMA_WORDS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "$vocabulary",
        "$id",
        "$anchor",
        "$dynamicanchor",
        "$ref",
        "$dynamicref",
        "$comment",
        "type",
        "enum",
        "const",
        "multipleof",
        "maximum",
        "exclusivemaximum",
        "minimum",
        "exclusiveminimum",
        "maxlength",
        "minlength",
        "pattern",
        "maxitems",
        "minitems",
        "uniqueitems",
        "maxcontains",
        "mincontains",
        "maxproperties",
        "minproperties",
        "required",
        "dependentrequired",
        "format",
        "contentencoding",
        "contentmediatype",
        "title",
        "description",
        "default",
        "deprecated",
        "readonly",
        "writeonly",
        "examples",
        "example",
        "nullable",
        "discriminator",
        "propertyname",
        "mapping",
        "xml",
        "name",
        "namespace",
        "prefix",
        "attribute",
        "wrapped",
        "summary",
        "operationid",
        "operationref",
        "in",
        "style",
        "explode",
        "allowreserved",
        "allowemptyvalue",
        "scheme",
        "bearerformat",
        "openidconnecturl",
        "authorizationurl",
        "tokenurl",
        "refreshurl",
        "scopes",
        "url",
        "externalvalue",
        "termsofservice",
        "version",
        "tags",
        "security",
        "contenttype",
        "openapi",
        "jsonschemadialect",
        "swagger",
        "host",
        "basepath",
        "schemes",
        "consumes",
        "produces",
        "collectionformat",
    }
)


def _hidden(hidden: bool, key: str | None) -> bool:
    """Whether the values under *key* describe the API rather than hold data.

    A schema word hides its value and subtree; a secret-named key shows
    them again, at any depth, so ``example: {token: {value: ...}}`` is
    still looked at under ``token``.
    """
    if key is None:
        return hidden
    return (hidden or key.lower() in _SCHEMA_WORDS) and not is_secret_name(key)


@dataclass(slots=True)
class _Walk:
    """The text under scan, the values found so far and the aliases seen."""

    text: str
    hits: list[SecretValue] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)


def _item_hit(walk: _Walk, start: int, end: int, name: str) -> None:
    """Record the scalar at *start*..*end* inside a collection, unless it is a stub."""
    text = walk.text
    quoted = text[start] in "\"'"
    if quoted:
        inner = (start + 1, quoted_end(text, start))
    else:
        core = text[start:end].split(" #", 1)[0].rstrip(" \t\r")
        if core.startswith("*"):
            walk.aliases.add(core[1:].split(None, 1)[0])
        inner = (start, start + len(core))
    value = text[inner[0] : inner[1]]
    if is_stub(value) or (not quoted and _NUMBER_RE.fullmatch(value.strip())):
        return
    walk.hits.append(SecretValue(inner[0], inner[1], "field", name))


def _opens_collection(rest: str) -> bool:
    """Whether *rest* starts with ``{`` or ``[`` and is not the placeholder itself."""
    return bool(rest) and rest[0] in "{[" and _PLACEHOLDER_RE.match(rest) is None


def _flow_token_end(text: str, pos: int) -> int:
    if text[pos] in "\"'":
        return min(quoted_end(text, pos) + 1, len(text))
    return scalar_end(text, pos, FLOW_STOP)


def _walk_flow(walk: _Walk, pos: int, name: str, hidden: bool) -> int:
    """Walk the ``{...}`` or ``[...]`` at *pos* and return where it ends.

    One pass over the characters: a key sets the name for the value that
    follows; a bracket opens a nested collection; a string or a bare word
    that is not a key is a value, named by its key or by the key of the
    list it sits in.
    """
    text = walk.text
    frames = [(text[pos], name, hidden)]
    pending: str | None = None
    i = pos + 1
    while i < len(text) and frames:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
        elif c == "#" or text.startswith("//", i):
            i = end_of_line(text, i)
        elif c in "{[":
            _, under, hidden = frames[-1]
            frames.append((c, pending or under, _hidden(hidden, pending)))
            pending = None
            i += 1
        elif c in "}]":
            frames.pop()
            i += 1
        elif frames[-1][0] == "{" and pending is None and (key := KEY_RE.match(text, i)):
            pending = decode_key(key)
            i = key.end()
        else:
            _, under, hidden = frames[-1]
            end = _flow_token_end(text, i)
            if not _hidden(hidden, pending):
                _item_hit(walk, i, end, pending or under)
            pending = None
            i = end
    return i


def _after_list_dashes(text: str, pos: int, line_end: int) -> int:
    """Return where the content of a line starts, past its ``- `` list dashes."""
    while pos < line_end and text[pos] == "-" and text[pos + 1 : pos + 2] in {" ", "\t", "\r", ""}:
        pos += 1
        while pos < line_end and text[pos] in " \t":
            pos += 1
    return pos


def _block_item(walk: _Walk, item: int, line_end: int, under: str, hidden: bool) -> int:
    """A line without a key: a flow collection, a scalar or a bare dash.

    Returns where the next line starts.
    """
    if item >= line_end:
        return line_end + 1
    if _opens_collection(walk.text[item:line_end]):
        return end_of_line(walk.text, _walk_flow(walk, item, under, hidden)) + 1
    if not hidden:
        _item_hit(walk, item, line_end, under)
    return line_end + 1


def _block_key(
    walk: _Walk, key: re.Match[str], column: int, line_end: int, hidden: bool
) -> tuple[int, tuple[int, str, bool] | None]:
    """A key line: walk or record its value.

    Returns the next line start and, when the key's value is on the lines
    below it, the frame to open: the key's column, name and whether it
    hides the values under it.
    """
    text = walk.text
    name = decode_key(key)
    key_hidden = _hidden(hidden, name)
    rest = text[key.end() : line_end].lstrip(" \t")
    if _opens_collection(rest):
        return end_of_line(text, _walk_flow(walk, line_end - len(rest), name, key_hidden)) + 1, None
    span = resolve_value(text, key.end(), column, "block", walk.aliases)
    if span is None:
        return line_end + 1, (column, name, key_hidden)
    if not key_hidden:
        walk.hits.append(SecretValue(span[0], span[1], "field", name))
    return end_of_line(text, span[1]) + 1, None


def _walk_block(walk: _Walk, start: int, end: int, name: str) -> None:
    """Walk the YAML lines *start*..*end* below a secret-named key.

    A line is a key with its value, a list item, a flow collection or a
    bare scalar. A key's children are the lines indented past the key;
    a stack of open keys says which key a line sits under and whether a
    schema word above hides it.
    """
    text = walk.text
    frames: list[tuple[int, str, bool]] = [(-1, name, False)]
    pos = start
    while pos < end:
        line_end = end_of_line(text, pos)
        line = text[pos:line_end]
        stripped = line.strip(" \t\r")
        if not stripped or is_comment(stripped):
            pos = line_end + 1
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        while frames[-1][0] >= indent:
            frames.pop()
        _, under, hidden = frames[-1]
        item = _after_list_dashes(text, pos + indent, line_end)
        key = KEY_RE.match(text, item)
        if key is None:
            pos = _block_item(walk, item, line_end, under, hidden)
            continue
        pos, frame = _block_key(walk, key, item - pos, line_end, hidden)
        if frame is not None:
            frames.append(frame)


def collection_hits(
    text: str, after: int, indent: int, mode: str, name: str, aliases: set[str]
) -> tuple[list[SecretValue], int]:
    """The scalars inside the object or list that the secret-named key *name* holds.

    *after* is where the key's separator ends; *indent* is the indentation
    of the key's line; *mode* is ``block`` (a key at the start of a line)
    or ``flow`` (a key after ``{`` or ``,``). An alias found as a value is
    added to *aliases*. Returns the hits and where the walked collection
    ends (*after* when the key holds no collection).
    """
    walk = _Walk(text, aliases=aliases)
    pos = after
    while True:
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        line_end = end_of_line(text, pos)
        rest = text[pos:line_end]
        if not rest or rest[0] not in "!&":
            break
        pos += len(rest.split(None, 1)[0])
    if _opens_collection(rest):
        return walk.hits, _walk_flow(walk, pos, name, False)
    if rest and not is_comment(rest):
        return walk.hits, after
    if mode == "block":
        children_end = deeper_lines_end(text, line_end, indent)
        if children_end > line_end:
            _walk_block(walk, line_end + 1, children_end, name)
            return walk.hits, children_end
        return walk.hits, after
    start = next_content_line(text, pos)
    if start is not None and _opens_collection(text[start : end_of_line(text, start)]):
        return walk.hits, _walk_flow(walk, start, name, False)
    return walk.hits, after
