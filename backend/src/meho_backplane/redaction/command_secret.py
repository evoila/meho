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
* a database login ``user/password@service`` (also ``@//host:port/service``),
  with quoted, joined and escaped password parts (``user/"x y"@service``);
* ``Password=``, ``Pwd=`` and ``pw=`` in a connection string. A value in
  quotes or in braces (``Pwd={x;y}``) is read to its closing mark on the
  same line. A value without them runs to the ``;`` (or the end of the line), so it may hold
  spaces and quotes (``Pwd=made'up;``). Text counts as a connection string
  when a ``;`` stands before the label, or a ``;`` ends the value and a
  next field (``;Database=``) follows or no option (``-x``) or ``key=``
  word stands in the value. A label behind ``-`` is a command option.
  Elsewhere ``password=`` takes one shell word, as an option does;
* environment style names that end in ``_PASS``, ``_PWD``, ``_PASSWORD``,
  ``_SECRET``, ``_TOKEN`` or ``_KEY`` (``DB_PASSWORD=x``);
* the text inside an XML element named like a secret
  (``<password>x</password>``), also the content of each CDATA section in
  it, with white space or other text around it.

A value is read the way a shell reads one word: it ends at white space or
at an operator (``; | & < > ( )``) that is not quoted or escaped. Quoted
parts, escaped characters (``\\ `` and ``\\"``) and several parts joined
together are one value, and a quoted part may run over a line break. A
quote that is never closed is a plain character inside a value; a value
that starts with one runs to the end of the line. A form that stands in a
quoted command (``sh -c "mysql -px"; echo "ok"``) ends at the closing
quote of that command. A quoted tool name (``"mysql" -px``) is not a
quoted command: its options follow the closing quote.

Every scan is linear. No pattern has a nested repeat, values are read by
position, a form that starts inside an earlier hidden value is not read
again, and a search to the end of the text (a quote or a CDATA section
that is never closed) is not repeated.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["COMMAND_SECRET", "CommandSecretFinder"]

_PLACEHOLDER: Final = re.compile(r"""\[REDACTED:[^\]\s'"]+\]""")

#: A run of bare value characters (no white space, quote, backslash or operator).
_BARE: Final = re.compile(r"""[^\s'"\\;|&<>()]+""")
_DOUBLE_QUOTE_MARK: Final = re.compile(r"""[\\"]""")


def _skip_double_quoted(text: str, start: int, size: int | None = None) -> int:
    """Index after the double quote that closes the one at *start*, or ``-1``."""
    size = len(text) if size is None else size
    pos = start + 1
    while True:
        mark = _DOUBLE_QUOTE_MARK.search(text, pos, size)
        if mark is None:
            return -1
        if text[mark.start()] == '"':
            return mark.end()
        pos = mark.start() + 2


def _value_end(text: str, start: int, size: int | None = None) -> int:
    """Index where the shell word that starts at *start* ends (``start`` when empty).

    *size* is where the reading stops, as the closing quote of a command
    that is quoted inside a longer command (``sh -c "mysql -px"``).
    """
    size = len(text) if size is None else size
    pos = start
    while pos < size:
        char = text[pos]
        bare = _BARE.match(text, pos, size)
        if bare is not None:
            pos = bare.end()
            continue
        if char == "\\":
            pos = min(pos + 2, size)
            continue
        if char in "'\"":
            closing = (
                text.find("'", pos + 1, size) + 1
                if char == "'"
                else _skip_double_quoted(text, pos, size)
            )
            if closing > 0:
                pos = closing
                continue
            if pos > start:
                # A quote inside a word that is never closed is a plain character.
                pos += 1
                continue
            line_end = text.find("\n", pos, size)
            return size if line_end == -1 else line_end
        break
    return pos


def _is_hidden(value: str) -> bool:
    """False for an empty value or a ``[REDACTED:...]`` placeholder."""
    return bool(value) and _PLACEHOLDER.fullmatch(value) is None


def _value_span(text: str, start: int, size: int | None = None) -> tuple[int, int] | None:
    """The span of the shell word at *start*, or ``None`` when it holds no secret."""
    end = _value_end(text, start, size)
    return (start, end) if _is_hidden(text[start:end]) else None


def _after(text: str, start: int, mark: str, size: int | None = None) -> tuple[int, int] | None:
    """The part of the shell word at *start* behind the first *mark* (``user:value``)."""
    end = _value_end(text, start, size)
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


# -- the quoted command a form stands in ------------------------------------------

_OUTSIDE_MARK: Final = re.compile(r"""['"\\\n]""")
_IN_SINGLE_MARK: Final = re.compile(r"['\n]")
_IN_DOUBLE_MARK: Final = re.compile(r'["\\\n]')


def _ends_word(text: str, index: int) -> bool:
    """True at the end of the text, at white space or at an operator."""
    return index >= len(text) or text[index].isspace() or text[index] in ";|&<>()"


class _OuterQuotes:
    """Where the quoted string that a position stands in closes, read left to right.

    ``sh -c "mysql -px"; echo "ok"`` quotes a command inside a longer one.
    The closing quote of that string ends the inner command, so it is not
    read as the start of a quoted part of the password. A quoted string
    ends at the end of its line here: log text often has an apostrophe of
    its own (``don't``). For the same reason a quote that a word follows
    directly opens a quoted value and does not close the string. Each part
    of the text is read once, so a scan stays linear.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._quote = ""
        self._close: int | None = None

    def _advance(self, index: int) -> None:
        text = self._text
        while self._pos < index:
            pattern = {"": _OUTSIDE_MARK, "'": _IN_SINGLE_MARK}.get(self._quote, _IN_DOUBLE_MARK)
            mark = pattern.search(text, self._pos, index)
            if mark is None:
                self._pos = index
                return
            char = mark.group(0)
            if char == "\\":
                self._pos = mark.start() + 2
                continue
            self._pos = mark.end()
            self._quote = "" if char == "\n" or self._quote else char
            self._close = None

    def end(self, index: int) -> int | None:
        """The closing quote of the string that *index* stands in, or ``None``."""
        self._advance(index)
        if not self._quote:
            return None
        if self._close is None:
            text = self._text
            line_end = text.find("\n", index)
            line_end = len(text) if line_end == -1 else line_end
            if self._quote == "'":
                self._close = text.find("'", index, line_end)
            else:
                after = _skip_double_quoted(text, index - 1, line_end)
                self._close = after - 1 if after > 0 else -1
            if self._close >= 0 and not _ends_word(text, self._close + 1):
                # A quote with a word right behind it opens a quoted value
                # (``don't --password 'a b'``): it does not close a string.
                self._close = -1
        return self._close if self._close >= 0 else None


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
    quotes = _OuterQuotes(text)
    pos = 0
    while (match := _LONG_OPTION.search(text, pos)) is not None:
        pos = match.end()
        if not _is_secret_option(match["name"]):
            continue
        if match["sep"] != "=" and text.startswith("--", match.end()):
            continue
        span = _value_span(text, match.end(), quotes.end(match.start()))
        if span is not None:
            spans.append(span)
            pos = span[1]
    return spans


# -- short options after a tool name --------------------------------------------

_TOOL: Final = re.compile(
    r"(?<![\w.-])(?:sshpass|mysql[a-z]{0,20}|mariadb[a-z-]{0,24}|sqlcmd|bcp|osql|nsrsqlsv"
    r"|ldap(?:search|modify|add|delete|compare|whoami)|curl|smbclient)(?![\w.:-])"
)
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


def _next_word_start(text: str, end: int, size: int) -> int | None:
    """Where the word after the one that ends at *end* starts, on the same line."""
    gap = _BLANKS.match(text, end, size)
    begin = end if gap is None else gap.end()
    if begin == end or begin >= size or text[begin] in "\n;|&":
        return None
    return begin


def _value_start(
    text: str, word: str, start: int, end: int, size: int, flags: tuple[str, ...]
) -> int | None:
    """Where the value of the option *word* starts, or ``None``.

    The value is the next word (``-w x``), or glued (``-wx``) for a short
    flag, or after ``=`` for a long flag (``--user=x``).
    """
    for flag in flags:
        if word == flag:
            return _next_word_start(text, end, size)
        if flag.startswith("--"):
            if word.startswith(flag + "="):
                return start + len(flag) + 1
        elif word.startswith(flag):
            return start + len(flag)
    return None


def _read(text: str, begin: int | None, mark: str | None, size: int) -> tuple[int, int] | None:
    """The secret span of the value at *begin*: all of it, or what follows *mark*."""
    if begin is None:
        return None
    return _value_span(text, begin, size) if mark is None else _after(text, begin, mark, size)


def _option_span(
    text: str, tools: set[str], word: str, span: tuple[int, int], size: int
) -> tuple[int, int] | None:
    """The secret value of one short option of the tools of this command."""
    start, end = span

    def value(flags: tuple[str, ...], mark: str | None = None) -> tuple[int, int] | None:
        return _read(text, _value_start(text, word, start, end, size, flags), mark, size)

    if "sshpass" in tools and word.startswith("-p"):
        tools.discard("sshpass")
        return value(("-p",))
    if "mysql" in tools and word.startswith("-p") and len(word) > 2:
        return _read(text, start + 2, None, size)
    if tools & _UPPER_P_TOOLS and word.startswith("-P"):
        return value(("-P",))
    if "ldap" in tools and word.startswith("-w"):
        return value(("-w",))
    if "curl" in tools and word.startswith(("-u", "--user")):
        return value(("-u", "--user"), ":")
    if "smbclient" in tools and word.startswith("-U"):
        return value(("-U",), "%")
    return None


def _segment_spans(
    text: str, first: int, tool: str, size: int
) -> tuple[list[tuple[int, int]], int]:
    """Read one command from the tool that ends at *first*; return spans and the end."""
    spans: list[tuple[int, int]] = []
    tools = {tool}
    pos = first
    skip_next = False
    while pos < size:
        gap = _BLANKS.match(text, pos, size)
        pos = pos if gap is None else gap.end()
        if pos >= size or text[pos] in "\n;|&":
            break
        # A whole shell word, so a ``;`` or a space inside quotes does not end it.
        start, end = pos, _value_end(text, pos, size)
        if end == start:
            # ``< > ( )`` is a word of its own.
            end += 1
        token = text[start:end]
        pos = end
        if skip_next:
            skip_next = False
            continue
        span = _option_span(text, tools, token, (start, end), size)
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


#: The quoted text in front of a quoted executable: its opening quote and a path.
_QUOTED_PATH: Final = re.compile(r"""['"][^\s'"]{0,256}$""")


def _is_quoted_executable(text: str, match: re.Match[str], close: int) -> bool:
    """True when the quotes hold only the tool name (``"mysql" -px``, ``'/bin/curl'``)."""
    if close != match.end():
        return False
    before = _QUOTED_PATH.search(text, max(0, match.start() - 257), match.start())
    if before is None or text[before.start()] != text[close]:
        return False
    path = before.group(0)[1:]
    return not path or path.endswith("/")


def _tool_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quotes = _OuterQuotes(text)
    pos = 0
    while (match := _TOOL.search(text, pos)) is not None:
        tool = _tool_name(match.group(0)) or ""
        close = quotes.end(match.start())
        first, size = match.end(), len(text) if close is None else close
        if close is not None and _is_quoted_executable(text, match, close):
            # The arguments follow the closing quote of the tool name.
            first, size = close + 1, len(text)
        found, pos = _segment_spans(text, first, tool, size)
        spans.extend(found)
        pos = max(pos, match.end())
    return spans


# -- database login user/password@service ---------------------------------------

_LOGIN_USER: Final = re.compile(r"(?<![\w/.:@$#-])[A-Za-z][\w$#-]{0,127}/")
#: Longest login password that is read; the bound keeps the scan linear.
_LOGIN_PASSWORD_MAX: Final = 256
#: A run of bare login password characters: no ``/``, ``@``, white space, quote or ``\``.
_LOGIN_BARE: Final = re.compile(r"""[^\s/@'"\\]+""")
_SERVICE: Final = re.compile(r"//[^\s'\"]{1,256}|[A-Za-z][\w.-]{0,127}")
#: What stands after ``@`` in text that is not a login: image digests
#: (``app@sha256:...``), versions and branches (``actions/checkout@v4``,
#: ``@main``), and commit ids.
_NOT_A_SERVICE: Final = re.compile(
    r"(?:sha\d+|v\d.*|main|master|latest|head|stable|[0-9a-f]{7,40})", re.IGNORECASE
)


def _login_password_end(text: str, start: int, size: int) -> int | None:
    """Index of the ``@`` that ends the login password at *start*, or ``None``.

    Bare characters, escaped characters and quoted parts are read, so
    ``user/"made up"@service`` is one password. A quoted part may hold
    ``/``, ``@`` and spaces.
    """
    window = text[start : min(size, start + _LOGIN_PASSWORD_MAX + 1)]
    pos = 0
    while pos < len(window):
        char = window[pos]
        bare = _LOGIN_BARE.match(window, pos)
        if bare is not None:
            pos = bare.end()
        elif char == "\\":
            pos += 2
        elif char in "'\"":
            closing = (
                window.find("'", pos + 1) + 1 if char == "'" else _skip_double_quoted(window, pos)
            )
            if closing <= 0:
                return None
            pos = closing
        elif char == "@" and pos > 0:
            return start + pos
        else:
            return None
    return None


def _login_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quotes = _OuterQuotes(text)
    pos = 0
    while (match := _LOGIN_USER.search(text, pos)) is not None:
        pos = match.end()
        close = quotes.end(match.start())
        at = _login_password_end(text, match.end(), len(text) if close is None else close)
        if at is None:
            continue
        service = _SERVICE.match(text, at + 1)
        if service is None:
            continue
        name = service.group(0)
        if not name.startswith("//") and _NOT_A_SERVICE.fullmatch(name.split(":")[0]):
            continue
        if _is_hidden(text[match.end() : at]):
            spans.append((match.end(), at))
            pos = at
    return spans


# -- connection string Password=...; ----------------------------------------------

_CONNECTION: Final = re.compile(
    r"(?<![A-Za-z0-9_])(?P<label>password|pwd|pw)[ \t]*=[ \t]*", re.IGNORECASE
)
#: Longest unquoted connection string value that is read. The bound keeps a text
#: full of ``pw=`` from being scanned again and again.
_VALUE_MAX: Final = 1024
#: An unquoted value runs to the ``;`` that ends it, so it may hold spaces and
#: quotes. A line break, or the closing quote of a quoted command the
#: connection string stands in, also ends it.
_VALUE_STOP: Final = re.compile(r"[\r\n;]")
#: A word that starts a new command argument: an option (``--user``, ``-v``) or a
#: ``key=`` word. A connection string value has no such word.
_COMMAND_WORD: Final = re.compile(r"\s(?:-{1,2}[A-Za-z]|[A-Za-z_][\w.-]{0,63}=)")
#: The next field of a connection string after the ``;`` (``;Database=``).
_NEXT_FIELD: Final = re.compile(r";[ \t]*[A-Za-z][\w .-]{0,63}=")
#: The closing character of a quoted connection string value. ODBC quotes a
#: value with braces (``Pwd={x;y}``).
_CLOSING: Final = {"'": "'", '"': '"', "{": "}"}
#: Longest quoted connection string value that is read across line breaks.
_QUOTED_MAX: Final = 128
#: A quoted value up to its closing mark, for each opening character. A doubled
#: closing mark is part of the value. The groups are atomic, so a value that is
#: never closed is read once and not tried again in another way.
_QUOTED_VALUE: Final = {
    opening: re.compile(
        f"[{re.escape(opening)}](?>[^{re.escape(closing)}]++|{re.escape(closing * 2)})*+"
        f"{re.escape(closing)}"
    )
    for opening, closing in _CLOSING.items()
}


def _quoted_connection_value(text: str, start: int) -> tuple[int, int]:
    """Span of a quoted connection string value; a doubled closing mark is part of it.

    A quoted value may hold line breaks. The search for the closing mark
    stops after ``_QUOTED_MAX`` characters. A value that is not closed there
    ends at the end of its first line, so it does not make each later label
    search to the end of the text.
    """
    limit = min(len(text), start + 1 + _QUOTED_MAX)
    value = _QUOTED_VALUE[text[start]].match(text, start, limit)
    if value is not None:
        return start, value.end()
    line_end = text.find("\n", start, limit)
    return start, len(text) if line_end < 0 else line_end


def _is_connection_field(text: str, match: re.Match[str], start: int, stop: int) -> bool:
    """True when the label at *match* is a field of a connection string.

    A label behind ``-`` is a command option. Otherwise a ``;`` before the
    label, a ``;`` and a next field after the value (``;Database=``), or a
    ``;`` that ends a value with no command word in it, marks a connection
    string.
    """
    if text[match.start() - 1 : match.start()] == "-":
        return False
    if text[max(0, match.start() - 40) : match.start()].rstrip().endswith(";"):
        return True
    if not text.startswith(";", stop):
        return False
    return (
        _NEXT_FIELD.match(text, stop) is not None or _COMMAND_WORD.search(text, start, stop) is None
    )


def _connection_span(
    text: str, match: re.Match[str], quotes: _OuterQuotes
) -> tuple[int, int] | None:
    start = match.end()
    if start >= len(text) or text[start] in ";\n":
        return None
    if text[start] in _CLOSING:
        return _quoted_connection_value(text, start)
    close = quotes.end(match.start())
    window = min(len(text) if close is None else close, start + _VALUE_MAX)
    stop = _VALUE_STOP.search(text, start, window)
    stop_at = window if stop is None else stop.start()
    if not _is_connection_field(text, match, start, stop_at):
        # A command option or word (``--password=x --user a``, ``password=x user=a``):
        # the value is one shell word, so the words after it stay. ``pwd`` and ``pw``
        # are connection string words only.
        if match["label"].lower() != "password":
            return None
        return _value_span(text, start, close)
    # A value hidden by an earlier rule is not part of this one.
    earlier = _PLACEHOLDER.search(text, start, stop_at)
    if earlier is not None:
        stop_at = earlier.start()
    end = start + len(text[start:stop_at].rstrip())
    return (start, end) if _is_hidden(text[start:end]) else None


def _connection_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quotes = _OuterQuotes(text)
    pos = 0
    while (match := _CONNECTION.search(text, pos)) is not None:
        pos = match.end()
        span = _connection_span(text, match, quotes)
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
    quotes = _OuterQuotes(text)
    pos = 0
    while (match := _ENVIRONMENT.search(text, pos)) is not None:
        pos = match.end()
        span = _value_span(text, match.end(), quotes.end(match.start()))
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
_CDATA_CLOSE: Final = "]]>"


def _is_secret_element(tag: str) -> bool:
    words = _name_words(tag)
    return bool(words) and (words[-1] in _XML_SECRET_LAST_WORDS or words[-2:] == ["api", "key"])


class _CdataEnds:
    """Find the next ``]]>``; a search is not repeated over text read before.

    Many CDATA starts without an end would each search to the end of the
    text. The last answer stays valid for every later start up to it.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._from = len(text) + 1
        self._found = -1

    def find(self, start: int) -> int:
        if not (self._from <= start and (self._found == -1 or self._found >= start)):
            self._from = start
            self._found = self._text.find(_CDATA_CLOSE, start)
        return self._found


def _trimmed(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The span *start*..*end* without white space at its ends, or ``None`` when empty."""
    part = text[start:end]
    core = part.strip()
    if not core or _PLACEHOLDER.fullmatch(core):
        return None
    lead = len(part) - len(part.lstrip())
    return start + lead, start + lead + len(core)


def _xml_text_spans(
    text: str, match: re.Match[str], cdata: _CdataEnds
) -> tuple[list[tuple[int, int]], int]:
    """The text and the CDATA content inside the element opened by *match*.

    Returns the spans and where the reading ended. The spans are empty when
    the element holds no text or is not closed right after its text. A
    ``<`` in the CDATA sections read here is not markup, so the next element
    is searched behind them: no section is read twice.
    """
    start = match.end()
    if text[start - 2] == "/":
        return [], start
    spans: list[tuple[int, int]] = []
    pos = start
    while True:
        tag = text.find("<", pos)
        if tag == -1:
            return [], start
        if (span := _trimmed(text, pos, tag)) is not None:
            spans.append(span)
        if not text.startswith(_CDATA_OPEN, tag):
            break
        inner = tag + len(_CDATA_OPEN)
        end = cdata.find(inner)
        if end == -1:
            return [], tag
        if (span := _trimmed(text, inner, end)) is not None:
            spans.append(span)
        pos = end + len(_CDATA_CLOSE)
    if not text.startswith("</" + match["tag"], tag):
        return [], tag
    return spans, tag


def _xml_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cdata = _CdataEnds(text)
    pos = 0
    while (match := _XML_OPEN.search(text, pos)) is not None:
        pos = match.end()
        if not _is_secret_element(match["tag"]):
            continue
        found, end = _xml_text_spans(text, match, cdata)
        spans.extend(found)
        pos = max(pos, end)
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
