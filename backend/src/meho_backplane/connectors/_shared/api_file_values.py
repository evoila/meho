# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The value walk shared by every format of :mod:`.api_file_secrets`.

:class:`Scrubber` replaces secret values in parsed data (what JSON and
YAML give, and what a text value that holds JSON gives) and keeps the
count of replacements. The markup writers in :mod:`.api_file_markup` use
it for text and attribute values; :mod:`.api_file_secrets` and
:mod:`.api_file_yaml` use it for whole JSON and YAML files.

A text value can hold JSON: a sample request in a description, often
with the request line before the body (``POST /sessions`` and then the
JSON). :func:`json_documents` finds every JSON object in a text (or list
that holds one), and :meth:`Scrubber.text` checks each one like a JSON
file and writes it back from the parsed copy.

The API file checks fail closed (``strict``): what the check cannot read
whole is withheld, never passed through. A text value is withheld when
its JSON names a key twice, when something in it starts like a JSON
object but does not parse, when it names a secret next to a value in
text that is not JSON (``sessionId: ...``, ``password=...``), or when it
is not valid Unicode. The withheld value becomes the placeholder; the
scrubber counts it and keeps the reason (:attr:`Scrubber.withheld`),
never the text. :exc:`WithheldError` carries such a reason.

A caller can give the scrubber one more rule for text values
(``text_rule``): a function that takes one text value and returns the
changed value and how many places changed. It runs on every text value
that is not replaced whole, so a rule that scans text (MEHO's general
secret-redaction rules, for example) changes the value before the file
is written back. The writer then quotes or escapes the changed value as
the format needs, and the file keeps its shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Final

from meho_backplane.broadcast.events import (
    is_secret_param_name,
    is_secret_param_name_lowercased,
)

__all__ = [
    "JSON_NOT_PARSED",
    "MORE_THAN_ONE_DOCUMENT",
    "NOT_PARSED",
    "NOT_UNICODE",
    "REDACTED",
    "REPEATED_KEY",
    "SECRET_CHILD_ELEMENT",
    "SECRET_NAME_AFTER_FLATTENING",
    "SECRET_NAME_IN_TEXT",
    "TOO_COSTLY",
    "TOO_DEEP",
    "ApiFileFormatError",
    "JsonDocument",
    "RepeatedKeyError",
    "Scrubber",
    "TextRule",
    "WithheldError",
    "dump_json",
    "is_secret_name",
    "is_secret_name_lowercased",
    "is_unicode",
    "json_documents",
    "load_json",
    "names_a_secret_with_a_value",
]

#: A rule for one text value: returns the changed value and the count of
#: places changed.
TextRule = Callable[[str], tuple[str, int]]

#: Written in place of every replaced value: the placeholder the governed
#: Vault read returns for a secret-named value, so a reader sees one
#: vocabulary.
REDACTED: Final[str] = "[REDACTED:param_name]"

_PEM_PRIVATE_KEY: Final[re.Pattern[str]] = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# Why a part of a file was withheld. The answer names the reason, never
# the text.
NOT_PARSED: Final[str] = "does not parse as one JSON or YAML document"
MORE_THAN_ONE_DOCUMENT: Final[str] = "holds more than one document"
REPEATED_KEY: Final[str] = "names a key twice"
JSON_NOT_PARSED: Final[str] = "holds JSON that does not parse"
SECRET_NAME_IN_TEXT: Final[str] = "names a secret next to a value in plain text"
SECRET_CHILD_ELEMENT: Final[str] = "has a child element that is secret or a script"
NOT_UNICODE: Final[str] = "holds text that is not valid Unicode"
TOO_DEEP: Final[str] = "is nested too deeply to check"
TOO_COSTLY: Final[str] = "is too costly to check"
#: The last check (:mod:`.api_file_last_check`) found a secret name with a
#: value once it read the text as a person reads it.
SECRET_NAME_AFTER_FLATTENING: Final[str] = (
    "names a secret next to a value once tags and escapes are removed"
)


#: The end of a name that holds a login session id (``sessionId``,
#: ``session_id``, ``X-Session-Id``, ``JSESSIONID``), with case, ``-``,
#: ``_`` and ``.`` removed. Some appliances use the session id as the
#: login token, so an API file example of it is a secret. MEHO's general
#: rule for secret names does not count it: there a ``session_id`` is
#: often MEHO's own agent session, which is no secret.
_SESSION_ID_NAME: Final[str] = "sessionid"


def _ends_in_session_id(name: str) -> bool:
    return (
        name.replace("-", "").replace("_", "").replace(".", "").lower().endswith(_SESSION_ID_NAME)
    )


def is_secret_name(name: str) -> bool:
    """True when the value under *name* is a secret in an API file.

    MEHO's general rule (:func:`is_secret_param_name`) plus a name that
    ends in a session id.
    """
    return is_secret_param_name(name) or _ends_in_session_id(name)


def is_secret_name_lowercased(name: str) -> bool:
    """:func:`is_secret_name` for a name whose case is lost (HTML)."""
    return is_secret_param_name_lowercased(name) or _ends_in_session_id(name)


class ApiFileFormatError(ValueError):
    """The file has no known format or does not parse. The message says why."""


class WithheldError(ValueError):
    """A part of the file cannot be checked whole and is withheld; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_unicode(text: str) -> bool:
    """True when *text* can be written as UTF-8 (no lone surrogate, such as JSON's ``\\ud800``)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


#: A name and what follows it when the text gives the name a value: a
#: closing quote (also an escaped one) and then ``:`` or ``=``
#: (``sessionId: ...``, ``"password": ...``, ``token=...``). The name
#: starts where a name can start and is read whole (``++``), so a long
#: run of name characters costs one pass, not one pass per character.
_NAME_WITH_A_VALUE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z0-9_.\-]++)\\?[\"']?\s*+[:=]"
)


def names_a_secret_with_a_value(text: str) -> bool:
    """True when *text* gives a secret name a value in a form only a parser could read.

    Plain text has no structure, so the check cannot tell where such a
    value ends. The name is checked with the rule for names whose case
    may be lost (:func:`is_secret_name_lowercased`), which matches more
    names, never fewer.
    """
    return any(
        is_secret_name_lowercased(match.group("name"))
        for match in _NAME_WITH_A_VALUE.finditer(text)
    )


def dump_json(data: Any) -> str:
    """Write *data* as compact JSON, close to the minified form appliances serve."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class RepeatedKeyError(ValueError):
    """One JSON object names a key twice; the parser would keep one value and drop the other."""


def load_json(text: str) -> Any:
    """Parse *text* as JSON, whole: an object that names a key twice is refused.

    :func:`json.loads` keeps the last value under a repeated key and drops
    the others without a word. A check of the parsed copy then says
    nothing about a dropped value, and a count of replacements stays at
    zero while the text still holds it. Raises :exc:`RepeatedKeyError`,
    a :exc:`ValueError`, for a key named twice in one object at any
    depth; any other :exc:`ValueError` is the parser's own.
    """
    return json.loads(text, object_pairs_hook=_object_without_repeats)


def _object_without_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise RepeatedKeyError(key)
        mapping[key] = value
    return mapping


#: One JSON document found in a text: ``(start, end, parsed)``.
JsonDocument = tuple[int, int, dict[str, Any] | list[Any]]

_JSON_DECODER: Final[json.JSONDecoder] = json.JSONDecoder(object_pairs_hook=_object_without_repeats)
_JSON_START: Final[re.Pattern[str]] = re.compile(r"[{\[]")

#: The start of a JSON object, or of a list that holds one: a ``{`` and
#: then a quoted name, or a ``[`` and then a ``{``. A start like this
#: that does not parse is JSON the check cannot read.
_JSON_OBJECT_START: Final[re.Pattern[str]] = re.compile(r"\{\s*[\"']|\[\s*\{")

#: How much text the JSON parser may read for one text value: this many
#: times the text's length, plus a floor for short texts. Text that is
#: JSON is read once; a bracket that opens no document costs a few
#: characters. A text that needs more (brackets that open over and over
#: and never close) is withheld.
_JSON_WORK_FACTOR: Final[int] = 4
_JSON_WORK_FLOOR: Final[int] = 4096


def json_documents(text: str, *, strict: bool = False) -> list[JsonDocument]:
    """Every JSON document in *text*, as ``(start, end, parsed)``, in order.

    A document is an object, or a list that holds an object: only a name
    marks a value as secret, so a list of scalars (``[1]``, a footnote)
    is text. Each ``{`` or ``[`` is tried as the start of a document. One
    that does not start a document is skipped by one character, so a
    document after a request line (``POST /sessions`` and then the body)
    is still found, and so is a path template (``/hosts/{id}``) skipped.

    Raises :exc:`WithheldError` when the text cannot be checked and must be
    withheld whole: a document names a key twice in one object
    (:func:`load_json` says why), or the parser read more text than the
    work budget allows. With *strict* (the API file checks) also when a
    start that looks like a JSON object (``{"``, ``[{``) does not parse,
    and when a document is too deep for the parser.
    """
    documents: list[JsonDocument] = []
    budget = _JSON_WORK_FACTOR * len(text) + _JSON_WORK_FLOOR
    position = 0
    while (match := _JSON_START.search(text, position)) is not None:
        start = match.start()
        try:
            parsed, end = _JSON_DECODER.raw_decode(text, start)
        except RepeatedKeyError:
            raise WithheldError(REPEATED_KEY) from None
        except json.JSONDecodeError as exc:
            if strict and _JSON_OBJECT_START.match(text, start):
                raise WithheldError(JSON_NOT_PARSED) from None
            budget -= max(exc.pos - start, 1)
            position = start + 1
        except RecursionError:
            if strict:
                raise WithheldError(TOO_DEEP) from None
            budget -= len(text) - start
            position = start + 1
        else:
            budget -= end - start
            if _holds_object(parsed):
                documents.append((start, end, parsed))
                position = end
            else:
                position = start + 1
        if budget < 0:
            raise WithheldError(TOO_COSTLY)
    return documents


def _holds_object(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and any(_holds_object(item) for item in value)


class Scrubber:
    """Replace secret values in parsed data; keep the count and the reasons for withheld parts.

    *strict* turns on the fail-closed rules for text values that the API
    file checks use (:meth:`text`). Other callers (the Salt connector)
    keep the plain walk.
    """

    def __init__(
        self, count: int = 0, *, text_rule: TextRule | None = None, strict: bool = False
    ) -> None:
        self.count = count
        #: Why each withheld part was withheld, in order; never its text.
        self.withheld: list[str] = []
        self._text_rule = text_rule
        self._strict = strict

    def fresh(self) -> Scrubber:
        """A scrubber with the same rules and a count of zero, to check one part on its own."""
        return Scrubber(text_rule=self._text_rule, strict=self._strict)

    def absorb(self, other: Scrubber) -> None:
        """Add the count and the withheld reasons of *other*, a part that passed its check."""
        self.count += other.count
        self.withheld.extend(other.withheld)

    def withhold(self, reason: str) -> str:
        """Count one withheld part, keep *reason*, and return the placeholder for it."""
        self.count += 1
        self.withheld.append(reason)
        return REDACTED

    def tree(self, node: Any, memo: dict[int, Any]) -> Any:
        """Return a copy of *node* with every secret value replaced.

        *memo* keeps one copy per container, so a container the file
        shares (a YAML alias) is walked once and stays shared in the
        copy; the YAML writer then writes an alias for it, not another
        copy.
        """
        if isinstance(node, dict):
            if id(node) in memo:
                return memo[id(node)]
            mapping: dict[Any, Any] = {}
            memo[id(node)] = mapping
            for key, value in node.items():
                if is_secret_name(str(key)) and value != REDACTED:
                    mapping[key] = REDACTED
                    self.count += 1
                else:
                    mapping[key] = self.tree(value, memo)
            return mapping
        if isinstance(node, (list, tuple)):
            if id(node) in memo:
                return memo[id(node)]
            items: list[Any] = []
            memo[id(node)] = items
            items.extend(self.tree(item, memo) for item in node)
            return items
        if isinstance(node, str):
            return self.text(node)
        return node

    def text(self, value: str) -> str:
        """Check one text value: a PEM private key, every JSON document in the text, the text rule.

        Each JSON document in the text (:func:`json_documents`: the body
        after a request line, a document inside other text) comes back
        written from the parsed copy, also when nothing was replaced; the
        text around the documents goes through the text rule. The text
        rule runs on the values inside the JSON, not on the written JSON
        text, so the text stays JSON.

        A text the check cannot read whole is withheld: the placeholder,
        counted, with the reason in :attr:`withheld`. That is a text
        whose JSON names a key twice in one object, at any depth (the
        parser would keep one value and drop the other), or that the
        parser cannot finish within its work budget. With *strict* it is
        also a text that is not valid Unicode, a text with a start that
        looks like a JSON object but does not parse, and a text that
        names a secret next to a value outside its JSON documents
        (:func:`names_a_secret_with_a_value`).
        """
        if _PEM_PRIVATE_KEY.search(value):
            self.count += 1
            return REDACTED
        if self._strict and not is_unicode(value):
            return self.withhold(NOT_UNICODE)
        try:
            documents = json_documents(value, strict=self._strict)
            if self._strict:
                _refuse_secret_names_in_text(value, documents)
        except WithheldError as exc:
            return self.withhold(exc.reason)
        out: list[str] = []
        position = 0
        for start, end, parsed in documents:
            if start > position:
                out.append(self._rule(value[position:start]))
            out.append(dump_json(self.tree(parsed, {})))
            position = end
        if position < len(value):
            out.append(self._rule(value[position:]))
        return "".join(out)

    def _rule(self, value: str) -> str:
        if self._text_rule is None:
            return value
        changed, count = self._text_rule(value)
        self.count += count
        return changed


def _refuse_secret_names_in_text(value: str, documents: list[JsonDocument]) -> None:
    """Raise :exc:`WithheldError` when text outside *documents* names a secret next to a value."""
    position = 0
    for start, end, _ in documents:
        if names_a_secret_with_a_value(value[position:start]):
            raise WithheldError(SECRET_NAME_IN_TEXT)
        position = end
    if names_a_secret_with_a_value(value[position:]):
        raise WithheldError(SECRET_NAME_IN_TEXT)
