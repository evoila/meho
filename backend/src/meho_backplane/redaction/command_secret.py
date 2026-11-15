# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find a password that stands on a command line, in text.

Log text, journal text and process lists show commands as they were
typed. The key=value rules do not know the forms that command lines use.
This finder reports only the secret value of each of these forms, so the
option name and the words around it stay readable:

* a long option with a secret-like name, with a space or ``=``
  (``--password x``, ``--token=x``, ``--api-key x``, ``--client-secret x``);
* short options, only after the tool name on the same line:
  ``sshpass -p x``, ``mysql -px`` (also ``mysqldump``, ``mariadb``),
  ``sqlcmd`` / ``bcp`` / ``osql`` / ``nsrsqlsv`` with ``-P x``,
  ``ldapsearch`` / ``ldapmodify`` with ``-w x``, ``curl -u user:x``,
  ``smbclient -U user%x``;
* a database login ``user/password@service`` (also ``@//host:port/service``);
* ``Password=``, ``Pwd=`` and ``pw=`` in a connection string;
* environment style names that end in ``_PASS``, ``_PWD``, ``_PASSWORD``,
  ``_SECRET``, ``_TOKEN`` or ``_KEY`` (``DB_PASSWORD=x``);
* the text inside an XML element named like a secret
  (``<password>x</password>``).

A value is read the way a shell reads one word: it ends at white space or
at an operator (``; | & < > ( )``) that is not quoted or escaped. Quoted
parts, escaped characters (``\\ `` and ``\\"``) and several parts joined
together are one value, and a quoted part may run over a line break. A
quote that is never closed ends the value in front of it, or, when the
value starts with it, at the end of the line.

Every scan is linear. No pattern has a nested repeat, values are read by
position, and a form that starts inside an earlier hidden value is not
read again.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["COMMAND_SECRET", "CommandSecretFinder"]

_PLACEHOLDER: Final = re.compile(r"""\[REDACTED:[^\]\s'"]+\]""")

#: A run of bare value characters (no white space, quote, backslash or operator).
_BARE: Final = re.compile(r"""[^\s'"\\;|&<>()]+""")
_DOUBLE_QUOTE_MARK: Final = re.compile(r"""[\\"]""")


def _skip_double_quoted(text: str, start: int) -> int:
    """Index after the double quote that closes the one at *start*, or ``-1``."""
    pos = start + 1
    while True:
        mark = _DOUBLE_QUOTE_MARK.search(text, pos)
        if mark is None:
            return -1
        if text[mark.start()] == '"':
            return mark.end()
        pos = mark.start() + 2


def _value_end(text: str, start: int) -> int:
    """Index where the shell word that starts at *start* ends (``start`` when empty)."""
    size = len(text)
    pos = start
    while pos < size:
        char = text[pos]
        bare = _BARE.match(text, pos)
        if bare is not None:
            pos = bare.end()
            continue
        if char == "\\":
            pos = min(pos + 2, size)
            continue
        if char in "'\"":
            closing = text.find("'", pos + 1) + 1 if char == "'" else _skip_double_quoted(text, pos)
            if closing > 0:
                pos = closing
                continue
            if pos > start:
                return pos
            line_end = text.find("\n", pos)
            return size if line_end == -1 else line_end
        break
    return pos


def _is_hidden(value: str) -> bool:
    """False for an empty value or a ``[REDACTED:...]`` placeholder."""
    return bool(value) and _PLACEHOLDER.fullmatch(value) is None


def _value_span(text: str, start: int) -> tuple[int, int] | None:
    """The span of the shell word at *start*, or ``None`` when it holds no secret."""
    end = _value_end(text, start)
    return (start, end) if _is_hidden(text[start:end]) else None


def _after(text: str, start: int, mark: str) -> tuple[int, int] | None:
    """The part of the shell word at *start* behind the first *mark* (``user:value``)."""
    end = _value_end(text, start)
    word = text[start:end]
    at = word.find(mark)
    if at == -1:
        return None
    begin = start + at + 1
    if len(word) > 1 and word[0] in "'\"" and word[-1] == word[0]:
        end -= 1
    if begin >= end or not _is_hidden(text[begin:end]):
        return None
    return (begin, end)


# -- long options -------------------------------------------------------------

_LONG_OPTION: Final = re.compile(r"(?<![\w-])--(?P<name>[A-Za-z][\w.-]{0,63})(?P<sep>=|[ \t]+)")
_NAME_SPLIT: Final = re.compile(r"[^A-Za-z0-9]+")
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SECRET_LAST_WORDS: Final = frozenset(
    {"password", "passwd", "pass", "pwd", "passphrase", "token", "secret"}
)


def _name_words(name: str) -> list[str]:
    return [word for word in _NAME_SPLIT.split(_CAMEL.sub("_", name).lower()) if word]


def _is_secret_option(name: str) -> bool:
    words = _name_words(name)
    if not words or words[0] == "no":
        return False
    return words[-1] in _SECRET_LAST_WORDS or words[-2:] == ["api", "key"]


def _long_option_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while (match := _LONG_OPTION.search(text, pos)) is not None:
        pos = match.end()
        if not _is_secret_option(match["name"]):
            continue
        if match["sep"] != "=" and text.startswith("--", match.end()):
            continue
        span = _value_span(text, match.end())
        if span is not None:
            spans.append(span)
            pos = span[1]
    return spans


# -- short options after a tool name --------------------------------------------

_TOOL: Final = re.compile(
    r"(?<![\w.-])(?:sshpass|mysql[a-z]{0,20}|mariadb[a-z-]{0,24}|sqlcmd|bcp|osql|nsrsqlsv"
    r"|ldap(?:search|modify|add|delete|compare|whoami)|curl|smbclient)(?![\w.:-])"
)
_WORD: Final = re.compile(r"[^\s;|&]+")
_BLANKS: Final = re.compile(r"[ \t]*")
_UPPER_P_TOOLS: Final = frozenset({"sqlcmd", "bcp", "osql", "nsrsqlsv"})
_SSHPASS_VALUE_OPTIONS: Final = frozenset({"-f", "-d", "-P"})


def _tool_name(word: str) -> str | None:
    """The tool a command word names, with its family folded in, or ``None``."""
    match = _TOOL.fullmatch(word.rsplit("/", 1)[-1])
    if match is None:
        return None
    name = match.group(0)
    if name.startswith(("mysql", "mariadb")):
        return "mysql"
    return "ldap" if name.startswith("ldap") else name


def _next_word_start(text: str, end: int) -> int | None:
    """Where the word after the one that ends at *end* starts, on the same line."""
    gap = _BLANKS.match(text, end)
    begin = end if gap is None else gap.end()
    if begin == end or begin >= len(text) or text[begin] in "\n;|&":
        return None
    return begin


def _value_start(text: str, word: str, start: int, end: int, flags: tuple[str, ...]) -> int | None:
    """Where the value of the option *word* starts, or ``None``.

    The value is the next word (``-w x``), or glued (``-wx``) for a short
    flag, or after ``=`` for a long flag (``--user=x``).
    """
    for flag in flags:
        if word == flag:
            return _next_word_start(text, end)
        if flag.startswith("--"):
            if word.startswith(flag + "="):
                return start + len(flag) + 1
        elif word.startswith(flag):
            return start + len(flag)
    return None


def _read(text: str, begin: int | None, mark: str | None) -> tuple[int, int] | None:
    """The secret span of the value at *begin*: all of it, or what follows *mark*."""
    if begin is None:
        return None
    return _value_span(text, begin) if mark is None else _after(text, begin, mark)


def _option_span(
    text: str, tools: set[str], word: str, start: int, end: int
) -> tuple[int, int] | None:
    """The secret value of one short option of the tools of this command."""
    if "sshpass" in tools and word.startswith("-p"):
        tools.discard("sshpass")
        return _read(text, _value_start(text, word, start, end, ("-p",)), None)
    if "mysql" in tools and word.startswith("-p") and len(word) > 2:
        return _read(text, start + 2, None)
    if tools & _UPPER_P_TOOLS and word.startswith("-P"):
        return _read(text, _value_start(text, word, start, end, ("-P",)), None)
    if "ldap" in tools and word.startswith("-w"):
        return _read(text, _value_start(text, word, start, end, ("-w",)), None)
    if "curl" in tools and word.startswith(("-u", "--user")):
        return _read(text, _value_start(text, word, start, end, ("-u", "--user")), ":")
    if "smbclient" in tools and word.startswith("-U"):
        return _read(text, _value_start(text, word, start, end, ("-U",)), "%")
    return None


def _segment_spans(text: str, first: int, tool: str) -> tuple[list[tuple[int, int]], int]:
    """Read one command line from the tool that ends at *first*; return spans and the end."""
    spans: list[tuple[int, int]] = []
    tools = {tool}
    size = len(text)
    pos = first
    skip_next = False
    while pos < size:
        gap = _BLANKS.match(text, pos)
        pos = pos if gap is None else gap.end()
        word = _WORD.match(text, pos) if pos < size else None
        if word is None:
            break
        start, end = word.span()
        token = word.group(0)
        pos = end
        if skip_next:
            skip_next = False
            continue
        span = _option_span(text, tools, token, start, end)
        if span is not None:
            spans.append(span)
            pos = max(pos, span[1])
        elif "sshpass" in tools and token in _SSHPASS_VALUE_OPTIONS:
            skip_next = True
        elif (name := _tool_name(token)) is not None:
            tools.add(name)
        elif "sshpass" in tools and not token.startswith("-"):
            tools.discard("sshpass")
    return spans, pos


def _tool_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while (match := _TOOL.search(text, pos)) is not None:
        tool = _tool_name(match.group(0)) or ""
        found, pos = _segment_spans(text, match.end(), tool)
        spans.extend(found)
        pos = max(pos, match.end())
    return spans


# -- database login user/password@service ---------------------------------------

_LOGIN: Final = re.compile(
    r"(?<![\w/.:@$#-])[A-Za-z][\w$#-]{0,127}/(?P<pw>[^\s/@'\"]{1,256})@"
    r"(?P<service>//[^\s'\"]{1,256}|[A-Za-z][\w.-]{0,127})"
)
#: What stands after ``@`` in text that is not a login: image digests
#: (``app@sha256:...``), versions and branches (``actions/checkout@v4``,
#: ``@main``), and commit ids.
_NOT_A_SERVICE: Final = re.compile(
    r"(?:sha\d+|v\d.*|main|master|latest|head|stable|[0-9a-f]{7,40})", re.IGNORECASE
)


def _login_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _LOGIN.finditer(text):
        service = match["service"]
        if not service.startswith("//") and _NOT_A_SERVICE.fullmatch(service.split(":")[0]):
            continue
        if _is_hidden(match["pw"]):
            spans.append(match.span("pw"))
    return spans


# -- connection string Password=...; ----------------------------------------------

_CONNECTION: Final = re.compile(
    r"(?<![A-Za-z0-9_])(?P<label>password|pwd|pw)[ \t]*=[ \t]*", re.IGNORECASE
)
#: Longest unquoted connection string value that is read. The bound keeps a text
#: full of ``pw=`` from being scanned again and again.
_VALUE_MAX: Final = 1024
_VALUE_STOP: Final = re.compile(r"""[\s;'"]""")


def _quoted_connection_value(text: str, start: int) -> tuple[int, int]:
    """Span of a quoted connection string value; a doubled quote is part of it."""
    quote = text[start]
    pos = start + 1
    while True:
        close = text.find(quote, pos)
        if close == -1:
            line_end = text.find("\n", start)
            return start, len(text) if line_end == -1 else line_end
        if text.startswith(quote, close + 1):
            pos = close + 2
            continue
        return start, close + 1


def _connection_span(text: str, match: re.Match[str]) -> tuple[int, int] | None:
    start = match.end()
    if start >= len(text) or text[start] in ";\n":
        return None
    if text[start] in "'\"":
        return _quoted_connection_value(text, start)
    window = start + _VALUE_MAX
    stop = _VALUE_STOP.search(text, start, window)
    end = min(len(text), window) if stop is None else stop.start()
    if match["label"].lower() != "password":
        # ``pwd`` and ``pw`` are connection string words only between semicolons.
        before = text[max(0, match.start() - 40) : match.start()].rstrip()
        if not (before.endswith(";") or text.startswith(";", end)):
            return None
    return (start, end) if _is_hidden(text[start:end]) else None


def _connection_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while (match := _CONNECTION.search(text, pos)) is not None:
        pos = match.end()
        span = _connection_span(text, match)
        if span is not None:
            spans.append(span)
            pos = max(pos, span[1])
    return spans


# -- environment style names ------------------------------------------------------

_ENVIRONMENT: Final = re.compile(
    r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{0,127}_(?:PASS|PWD|PASSWORD|SECRET|TOKEN|KEY)="
)


def _environment_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while (match := _ENVIRONMENT.search(text, pos)) is not None:
        pos = match.end()
        span = _value_span(text, match.end())
        if span is not None:
            spans.append(span)
            pos = span[1]
    return spans


# -- XML elements named like a secret ----------------------------------------------

_XML_OPEN: Final = re.compile(r"<(?P<tag>[A-Za-z_][\w.:-]{0,63})(?:\s[^<>]{0,256})?>")
_XML_SECRET_LAST_WORDS: Final = frozenset(
    {"password", "passwd", "pwd", "passphrase", "secret", "token", "apikey"}
)
_CDATA_OPEN: Final = "<![CDATA["


def _is_secret_element(tag: str) -> bool:
    words = _name_words(tag)
    return bool(words) and (words[-1] in _XML_SECRET_LAST_WORDS or words[-2:] == ["api", "key"])


def _xml_text_span(text: str, match: re.Match[str]) -> tuple[int, int] | None:
    """The text inside the element opened by *match*, or ``None`` when it holds no text."""
    start = match.end()
    if text[start - 2] == "/":
        return None
    if text.startswith(_CDATA_OPEN, start):
        start += len(_CDATA_OPEN)
        end = text.find("]]>", start)
        closing = end + 3
    else:
        end = text.find("<", start)
        closing = end
    if end == -1 or not text.startswith("</" + match["tag"], closing):
        return None
    value = text[start:end]
    if not value.strip() or _PLACEHOLDER.fullmatch(value.strip()):
        return None
    lead = len(value) - len(value.lstrip())
    return start + lead, end - (len(value) - len(value.rstrip()))


def _xml_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while (match := _XML_OPEN.search(text, pos)) is not None:
        pos = match.end()
        if not _is_secret_element(match["tag"]):
            continue
        span = _xml_text_span(text, match)
        if span is not None:
            spans.append(span)
            pos = span[1]
    return spans


# -- the finder -------------------------------------------------------------------


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort the spans and join those that touch or overlap."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class CommandSecretFinder:
    """Report the secret value of each command line form listed in the module docstring."""

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret value, left to right, never overlapping."""
        return _merge(
            [
                *_long_option_spans(text),
                *_tool_spans(text),
                *_login_spans(text),
                *_connection_spans(text),
                *_environment_spans(text),
                *_xml_spans(text),
            ]
        )


COMMAND_SECRET: Final[CommandSecretFinder] = CommandSecretFinder()
