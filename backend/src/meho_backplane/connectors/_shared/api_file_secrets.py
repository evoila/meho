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
   :func:`~meho_backplane.broadcast.events.is_secret_param_name`, or
   when it ends in a session id (``sessionId``): some appliances use the
   session id as the login token
   (:func:`~meho_backplane.connectors._shared.api_file_values.is_secret_name`).

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
   * Every JSON object in a text value (a sample request in a
     description, the body after a request line) is parsed and checked
     the same way, and written back from the parsed copy.
   * A text value that holds a private key in PEM form, under any name.

   The check fails closed: what it cannot read whole is withheld, never
   passed through. A withheld part becomes the placeholder, is counted,
   and its reason is kept in :attr:`CheckedApiFile.withheld`, never its
   text (:mod:`.api_file_values`). In a text value that is JSON that
   names a key twice, a start that looks like JSON but does not parse,
   a secret name next to a value in plain text (``sessionId: ...``) and
   text that is not valid Unicode. An HTML ``<pre>`` or ``<code>`` block
   is an example: it comes back only as one JSON or YAML document parsed
   whole, or as plain text, and is withheld otherwise
   (:mod:`.api_file_markup`).

4. Runs the caller's text rule, when one is given, on every text value
   that was not replaced whole (``text_rule``: one text value in, the
   changed value and the count of places changed out). The SRM read
   gives MEHO's general secret-redaction rules here, so an
   ``Authorization: Basic ...`` example in a description comes back as
   a placeholder. The rule sees the parsed value, before the file is
   written back, so the writer quotes the changed value as the format
   needs and no value changes its type.
5. Writes the checked copy in the same format and counts the replaced
   values.
6. Runs the last check on the copy (:mod:`.api_file_last_check`). It
   reads the copy the way a person reads it: tags removed and text joined
   in document order, HTML entities, backslash escapes and URL percent
   escapes decoded, and every JSON or YAML document inside a text parsed.
   If a secret name still has a value, that part is held back whole and
   counted, with its reason. This check only holds back more. It then
   runs once more on its own result; a copy that still shows a secret, or
   that is too costly to read this way, is refused.
7. Refuses a copy that cannot be written as UTF-8 (a key with a lone
   surrogate).

:func:`scrub_query_string` does the same for the query of a redirect
path: the names are decoded first (``license%4Bey`` reads as
``licenseKey``), and a secret-named value is replaced. The last check
reads the whole path after that (``;jsessionid=...`` in a path segment, a
name encoded twice); a path that still shows a secret becomes the
placeholder.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode

from meho_backplane.connectors._shared.api_file_last_check import (
    LastCheckTooCostlyError,
    Work,
    last_check_json,
    secret_in_text,
)
from meho_backplane.connectors._shared.api_file_markup import (
    check_html,
    check_xml,
    last_check_html,
    last_check_xml,
)
from meho_backplane.connectors._shared.api_file_values import (
    REDACTED,
    ApiFileFormatError,
    Scrubber,
    TextRule,
    dump_json,
    is_secret_name,
    is_unicode,
)
from meho_backplane.connectors._shared.api_file_yaml import check_yaml, last_check_yaml

__all__ = [
    "REDACTED",
    "ApiFileFormatError",
    "CheckedApiFile",
    "TextRule",
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


@dataclass(frozen=True, slots=True)
class CheckedApiFile:
    """The checked copy of a file, how many values were replaced, and what was withheld.

    ``secrets_replaced`` counts every placeholder in the copy, the
    withheld parts too. ``withheld`` holds one reason for each part the
    check could not read whole and withheld (an example block, a text
    value), in order; never the text.
    """

    text: str
    secrets_replaced: int
    withheld: tuple[str, ...] = ()


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


def check_api_file(
    text: str, *, content_type: str | None, path: str, text_rule: TextRule | None = None
) -> CheckedApiFile:
    """Parse *text*, replace every secret value, write the copy back.

    *text_rule*, when given, runs on every text value that is not
    replaced whole; its count joins ``secrets_replaced``. Raises
    :exc:`ApiFileFormatError` when the format is unknown or the file
    does not parse. An empty file comes back empty.
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
        copy, scrubber = _CHECKERS[fmt](text, path, text_rule)
        copy = _last_check(copy, fmt, path, scrubber)
    except RecursionError:
        raise ApiFileFormatError(f"{path} is nested too deeply to check; not returned") from None
    if not is_unicode(copy):
        raise ApiFileFormatError(f"{path} holds text that is not valid Unicode; not returned")
    return CheckedApiFile(copy, scrubber.count, tuple(scrubber.withheld))


def _last_check(copy: str, fmt: str, path: str, scrubber: Scrubber) -> str:
    """*copy* after the last check (:mod:`.api_file_last_check`), which counts on *scrubber*.

    When the check held back a part, it runs once more on its own result
    and must find nothing; otherwise the file is refused. A copy that is
    too costly to read this way is refused too.
    """
    work = Work(len(copy))
    before = scrubber.count
    try:
        checked = _LAST_CHECKS[fmt](copy, scrubber, work)
        if scrubber.count != before:
            again = scrubber.fresh()
            _LAST_CHECKS[fmt](checked, again, work)
            if again.count:
                raise ApiFileFormatError(
                    f"{path} still shows a secret after the last check; not returned"
                )
    except LastCheckTooCostlyError:
        raise ApiFileFormatError(f"{path} is too costly to check; not returned") from None
    return checked


def scrub_query_string(path: str) -> tuple[str, int]:
    """Replace secret-named values in the query of *path*; return it and the count.

    The names are decoded before the check, so an encoded name cannot hide
    a secret-named key. Then the last check reads the whole path as a
    person reads it (:func:`secret_in_text`): a path that still shows a
    secret name with a value becomes the placeholder. A path that holds no
    secret comes back as written.
    """
    base, separator, query = path.partition("?")
    count = 0
    if separator:
        pairs: list[tuple[str, str]] = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            if is_secret_name(key):
                pairs.append((key, REDACTED))
                count += 1
            else:
                pairs.append((key, value))
        if count:
            path = f"{base}?{urlencode(pairs, safe='[]:')}"
    try:
        shows_a_secret = secret_in_text(path, Work(len(path))) is not None
    except LastCheckTooCostlyError:
        shows_a_secret = True
    if shows_a_secret:
        return REDACTED, count + 1
    return path, count


# ---------------------------------------------------------------------------
# JSON: parsed with the stdlib, written back compact
# ---------------------------------------------------------------------------


def _check_json(text: str, path: str, text_rule: TextRule | None) -> tuple[str, Scrubber]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApiFileFormatError(
            f"{path} is not well-formed JSON ({exc.msg} at line {exc.lineno}, column "
            f"{exc.colno}); the file is not returned"
        ) from None
    scrubber = Scrubber(text_rule=text_rule, strict=True)
    copy = scrubber.tree(data, {})
    return dump_json(copy), scrubber


_CHECKERS: Final[dict[str, Callable[[str, str, TextRule | None], tuple[str, Scrubber]]]] = {
    "json": _check_json,
    "yaml": check_yaml,
    "xml": check_xml,
    "html": check_html,
}

_LAST_CHECKS: Final[dict[str, Callable[[str, Scrubber, Work], str]]] = {
    "json": last_check_json,
    "yaml": last_check_yaml,
    "xml": last_check_xml,
    "html": last_check_html,
}
