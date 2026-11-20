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
  with quoted, joined and escaped password parts (``user/"x y"@service``).
  A service name that looks like a branch, a version or a commit id
  (``@main``, ``@v4``) is a repository reference, unless a database client
  (``sqlplus``, ``rman``, ``expdp``, ...) stands before it in the same command;
* ``Password=``, ``Pwd=`` and ``pw=`` in a connection string. A value in
  quotes or in braces (``Pwd={x;y}``) is read to its closing mark, also
  across line breaks; a value that is never closed ends at its line. A
  value without them runs to the ``;`` that ends it, so it may hold spaces
  and quotes (``Pwd=made'up;``). With no ``;`` after it, it runs to the
  end of its line, so a last field may hold spaces. Text counts as a
  connection string when a ``;`` stands before the label, or a ``;`` ends
  the value and a next field (``;Database=``) follows or no option
  (``-x``) or ``key=`` word stands in the value. A label behind ``-`` is
  a command option. Only the value is hidden: the label and the next
  fields stay as the other rules leave them.
  Elsewhere ``password=`` takes one shell word, as an option does;
* environment style names that end in ``_PASS``, ``_PWD``, ``_PASSWORD``,
  ``_SECRET``, ``_TOKEN`` or ``_KEY`` (``DB_PASSWORD=x``, ``APP_KEY=x``);
* the text inside an XML element named like a secret
  (``<password>x</password>``), also the content of each CDATA section in
  it, with white space or other text around it.

A value is read the way a shell reads one word: it ends at white space or
at an operator (``; | & < > ( )``) that is not quoted or escaped. Quoted
parts, escaped characters (``\\ `` and ``\\"``) and several parts joined
together are one value, and a quoted part may run over a line break, also
when more parts follow it (``'made<line break>up'tail``). A quote that
closes more than 64 lines later is an apostrophe of a later log line
(``it's done``), and the quote is never closed. A quote that is never
closed is a plain character inside a value; a value that starts with one
runs to the end of the line. A form that stands in a quoted command
(``sh -c "mysql -px"; echo "ok"``) or in a JSON string
(``{"msg":"mysql -px","ok":1}``) ends at the closing quote of that
string. A quoted tool name (``"mysql" -px``) is not a quoted command: its
options follow the closing quote.

A double-quoted string with a backslash in it (a JSON string, or a quoted
command) is also read as its text: ``\\"`` is a quote and ``\\\\`` is one
backslash there. So ``{"msg":"run --password \\"made up\\""}`` hides
``\\"made up\\"`` whole. A string inside such a string is read the same
way, up to three levels deep.

The named pattern (:mod:`.patterns`) reports every value it reads. When the
``api_key`` rule runs right after this one in a policy, the engine uses
:data:`.patterns.COMMAND_SECRET_BEFORE_API_KEY` instead: a value that this rule
reads and that rule hides whole is left to it, so the answer stays as it
was. A value that the ``api_key`` rule hides only in part is hidden whole,
together with that part. So this rule changes an answer only where it
hides more of a secret value, and a policy without the ``api_key`` rule
still hides every value.

Names that look like a secret but are not one stay: paging tokens
(``--next-token``, ``<NextToken>``, ``<ContinuationToken>``), a short list
of ``_KEY`` names that hold no secret (``S3_KEY=``, ``PARTITION_KEY=``),
the compiler option ``-pthread`` and a working folder ``pwd=/tmp`` that is not
a field of a connection string.

Every scan is linear. No pattern has a nested repeat, values are read by
position, a form that starts inside an earlier hidden value is not read
again, and a search to the end of the text (a quote or a CDATA section
that is never closed) is not repeated.
"""

from __future__ import annotations

import bisect
import re
from typing import Final

__all__ = ["COMMAND_SECRET", "CommandSecretFinder"]

_PLACEHOLDER: Final = re.compile(r"""\[REDACTED:[^\[\]\s'"]+\]""")

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


#: Most line breaks a quoted value may hold. A quote further down belongs to
#: a later log line, so the value is not closed.
_MAX_VALUE_LINES: Final = 64


def _ends_word(text: str, index: int) -> bool:
    """True at the end of the text, at white space or at an operator."""
    return index >= len(text) or text[index].isspace() or text[index] in ";|&<>()"


def _ends_string(text: str, index: int) -> bool:
    """True where a closing quote may stand before *index*.

    The quote ends a word, or a JSON or list item follows (``"a","b"``,
    ``"a"}``), or another quoted string.
    """
    return _ends_word(text, index) or text[index] in ",:]}'\""


def _closes_value(text: str, opening: int, after: int) -> bool:
    """True when the quote that ends before *after* closes the one at *opening*.

    It does when it stands at most ``_MAX_VALUE_LINES`` lines further down,
    also at the end of the text.
    """
    return text.count("\n", opening, after) <= _MAX_VALUE_LINES


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
            if closing > 0 and _closes_value(text, pos, closing):
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


class _OuterQuotes:
    """Where the quoted string that a position stands in closes, read left to right.

    ``sh -c "mysql -px"; echo "ok"`` quotes a command inside a longer one.
    The closing quote of that string ends the inner command, so it is not
    read as the start of a quoted part of the password. A quoted string
    ends at the end of its line here: log text often has an apostrophe of
    its own (``don't``). For the same reason a quote that a word follows
    directly opens a quoted value and does not close the string; a ``,``,
    ``:``, ``]`` or ``}`` after the quote closes it (a JSON string). Each
    part of the text is read once, and the end of a line is found once, so
    a scan stays linear.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._quote = ""
        self._close: int | None = None
        self._line_end = -1

    def _end_of_line(self, index: int) -> int:
        if self._line_end < index:
            line_end = self._text.find("\n", index)
            self._line_end = len(self._text) if line_end == -1 else line_end
        return self._line_end

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
            line_end = self._end_of_line(index)
            if self._quote == "'":
                self._close = text.find("'", index, line_end)
            else:
                after = _skip_double_quoted(text, index - 1, line_end)
                self._close = after - 1 if after > 0 else -1
            if self._close >= 0 and not _ends_string(text, self._close + 1):
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
#: The word before ``token`` in the name of a paging token. Such a token names
#: the next page of a list and is passed back to read it, so it stays.
_PAGING_WORDS: Final = frozenset(
    {"next", "continuation", "page", "pagination", "starting", "start", "resume", "cursor"}
)


def _name_words(name: str) -> list[str]:
    return [word for word in _NAME_SPLIT.split(_CAMEL.sub("_", name).lower()) if word]


def _is_paging_token(words: list[str]) -> bool:
    """True for ``next token``, ``continuation token`` and the like."""
    return len(words) > 1 and words[-1] == "token" and words[-2] in _PAGING_WORDS


def _is_secret_option(name: str) -> bool:
    words = _name_words(name)
    if not words or words[0] == "no" or _is_paging_token(words):
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
    if "mysql" in tools and word.startswith("-p") and len(word) > 2 and word != "-pthread":
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


#: A database client: a login after it in the same command is a login, also
#: when its service name looks like a branch or a version (``@main``, ``@v19prod``).
_DATABASE_CLIENT: Final = re.compile(
    r"(?<![\w.-])(?:sqlplus|rman|expdp|impdp|exp|imp|sqlldr|dgmgrl)(?![\w.-])"
)
#: How far before a login a database client is looked for.
_CLIENT_WINDOW: Final = 512
_COMMAND_BREAK: Final = re.compile(r"[\n;|&]")


def _after_database_client(text: str, at: int) -> bool:
    """True when a database client stands before *at* in the same command."""
    begin = max(0, at - _CLIENT_WINDOW)
    breaks = [m.end() for m in _COMMAND_BREAK.finditer(text, begin, at)]
    return _DATABASE_CLIENT.search(text, breaks[-1] if breaks else begin, at) is not None


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
        if (
            not name.startswith("//")
            and _NOT_A_SERVICE.fullmatch(name.split(":")[0])
            and not _after_database_client(text, match.start())
        ):
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


class _ClosingMarks:
    """Find the closing mark of a quoted connection string value.

    A doubled closing mark is part of the value, and the value may hold line
    breaks. The runs of each closing character are found once for the text.
    Each search is then one look-up, so many values that are never closed do
    not each read to the end of the text.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._runs: dict[str, tuple[list[int], list[int], list[int]]] = {}
        self._newlines: list[int] | None = None

    def lines_between(self, start: int, end: int) -> int:
        """The number of line breaks between *start* and *end*, by one look-up."""
        if self._newlines is None:
            self._newlines = [m.start() for m in re.finditer("\n", self._text)]
        return bisect.bisect_left(self._newlines, end) - bisect.bisect_left(self._newlines, start)

    def _runs_of(self, closing: str) -> tuple[list[int], list[int], list[int]]:
        """Start and end of each run of *closing*, and the first odd run end from each run on."""
        if closing not in self._runs:
            runs = [m.span() for m in re.finditer(f"{re.escape(closing)}+", self._text)]
            starts = [a for a, _ in runs]
            ends = [b for _, b in runs]
            odd_end = [-1] * (len(runs) + 1)
            for i in range(len(runs) - 1, -1, -1):
                odd_end[i] = ends[i] if (ends[i] - starts[i]) % 2 else odd_end[i + 1]
            self._runs[closing] = (starts, ends, odd_end)
        return self._runs[closing]

    def end(self, start: int) -> int | None:
        """Index after the mark that closes the value opened at *start*, or ``None``.

        In a run of closing characters, pairs are part of the value. A run of
        odd length (counted from where the value starts) ends with the closing mark.
        """
        starts, ends, odd_end = self._runs_of(_CLOSING[self._text[start]])
        i = bisect.bisect_right(ends, start + 1)
        if i == len(ends):
            return None
        if (ends[i] - max(starts[i], start + 1)) % 2:
            return ends[i]
        return None if odd_end[i + 1] < 0 else odd_end[i + 1]


def _closed_value_end(text: str, start: int, marks: _ClosingMarks) -> int | None:
    """Index after the closing mark of the quoted value at *start*, or ``None``.

    ``None`` when the value is never closed, or closed more than
    ``_MAX_VALUE_LINES`` lines further down.
    """
    end = marks.end(start)
    if end is None:
        return None
    if text.find("\n", start, end) == -1 or marks.lines_between(start, end) <= _MAX_VALUE_LINES:
        return end
    return None


def _quoted_connection_value(text: str, start: int, marks: _ClosingMarks) -> tuple[int, int]:
    """Span of a quoted connection string value, through its closing mark.

    A value that is not closed (``_closed_value_end``) ends at the end of its line.
    """
    end = _closed_value_end(text, start, marks)
    if end is not None:
        return start, end
    line_end = text.find("\n", start)
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


def _is_working_folder(text: str, match: re.Match[str], start: int, stop: int) -> bool:
    """True for ``pwd=/tmp``: a folder path that is not a connection string field.

    A field stands before it (``Server=db;Pwd=/x``, also on the line above)
    or after it (``Pwd=/x;Database=d``) in a connection string.
    """
    if match["label"].lower() != "pwd" or not text.startswith("/", start):
        return False
    if _NEXT_FIELD.match(text, stop) is not None:
        return False
    before = text[max(0, match.start() - 256) : match.start()].rstrip()
    if not before.endswith(";"):
        return True
    field = before[:-1].rsplit("\n", 1)[-1].rsplit(";", 1)[-1]
    return "=" not in field


def _connection_span(
    text: str, match: re.Match[str], quotes: _OuterQuotes, marks: _ClosingMarks
) -> list[tuple[int, int]]:
    start = match.end()
    if start >= len(text) or text[start] in ";\n":
        return []
    close = quotes.end(match.start())
    limit = len(text) if close is None else close
    if text[start] in _CLOSING:
        # A value that the closing quote of a JSON string or a quoted command
        # cuts off ends there, so the string still closes.
        begin, end = _quoted_connection_value(text, start, marks)
        return [(begin, min(end, limit))]
    window = min(limit, start + _VALUE_MAX)
    stop = _VALUE_STOP.search(text, start, window)
    stop_at = window if stop is None else stop.start()
    if _is_working_folder(text, match, start, stop_at):
        return []
    if not _is_connection_field(text, match, start, stop_at):
        # A command option or word (``--password=x --user a``, ``password=x user=a``):
        # the value is one shell word, so the words after it stay. ``pwd`` and ``pw``
        # are connection string words only.
        if match["label"].lower() != "password":
            return []
        word = _value_span(text, start, close)
        return [] if word is None else [word]
    # A value hidden by an earlier rule is not part of this one.
    earlier = _PLACEHOLDER.search(text, start, stop_at)
    if earlier is not None:
        stop_at = earlier.start()
    end = start + len(text[start:stop_at].rstrip())
    return [(start, end)] if _is_hidden(text[start:end]) else []


def _connection_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quotes = _OuterQuotes(text)
    marks = _ClosingMarks(text)
    pos = 0
    while (match := _CONNECTION.search(text, pos)) is not None:
        pos = match.end()
        found = _connection_span(text, match, quotes, marks)
        spans.extend(found)
        if found:
            pos = max(pos, found[-1][1])
    return spans


# -- environment style names ------------------------------------------------------

_ENVIRONMENT: Final = re.compile(
    r"(?<![A-Za-z0-9_])(?P<name>[A-Z][A-Z0-9_]{0,127})_(?P<kind>PASS|PWD|PASSWORD|SECRET|TOKEN|KEY)="
)
#: The word before ``_KEY`` in a name that holds no secret: a path, a column,
#: a cache entry or a public key id (``S3_KEY``, ``PARTITION_KEY``,
#: ``GPG_KEY``). Every other ``_KEY`` name is hidden.
_PLAIN_KEY_WORDS: Final = frozenset(
    {
        "s3",
        "object",
        "partition",
        "sort",
        "primary",
        "foreign",
        "unique",
        "row",
        "range",
        "cache",
        "idempotency",
        "gpg",
        "pgp",
        "public",
    }
)


def _is_secret_environment_name(match: re.Match[str]) -> bool:
    last = match["name"].rsplit("_", 1)[-1].lower()
    if match["kind"] == "KEY":
        return last not in _PLAIN_KEY_WORDS
    return not (match["kind"] == "TOKEN" and last in _PAGING_WORDS)


def _environment_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quotes = _OuterQuotes(text)
    pos = 0
    while (match := _ENVIRONMENT.search(text, pos)) is not None:
        pos = match.end()
        if not _is_secret_environment_name(match):
            continue
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
    if not words or _is_paging_token(words):
        return False
    return words[-1] in _XML_SECRET_LAST_WORDS or words[-2:] == ["api", "key"]


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


#: How many levels of strings inside strings are read (a JSON log line in a
#: JSON string in a JSON document).
_MAX_STRING_DEPTH: Final = 3
_STRING_MARK: Final = re.compile(r'["\\\n]')
_ESCAPE: Final = re.compile(r"\\(?:u[0-9A-Fa-f]{4}|.)", re.DOTALL)
_ESCAPED_CHARS: Final = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _escaped_strings(text: str) -> list[tuple[int, int]]:
    """The inside of each double-quoted string on one line that holds a backslash.

    Read once from left to right: outside a string a ``"`` opens one,
    inside it a backslash takes the next character and a ``"`` closes it.
    A line break ends a string that is never closed.
    """
    found: list[tuple[int, int]] = []
    opening = -1
    escaped = False
    pos = 0
    while (mark := _STRING_MARK.search(text, pos)) is not None:
        at, char = mark.start(), mark.group(0)
        pos = at + 1
        if char == "\n":
            opening = -1
        elif char == "\\":
            if opening >= 0:
                escaped = True
                pos = at + 2
        elif opening < 0:
            opening, escaped = at, False
        else:
            if escaped:
                found.append((opening + 1, at))
            opening = -1
    return found


def _decoded(text: str, start: int, end: int) -> tuple[str, list[int]]:
    """The text of the string inside *start*..*end*, and where each character came from.

    ``starts[i]`` is the index in *text* where character ``i`` of the
    result starts; one more entry holds *end*. An escape that JSON does
    not know (``\\ ``) stays as it is.
    """
    parts: list[str] = []
    starts: list[int] = []
    pos = start
    for escape in _ESCAPE.finditer(text, start, end):
        parts.append(text[pos : escape.start()])
        starts.extend(range(pos, escape.start()))
        sequence = escape.group(0)
        if len(sequence) == 6:
            char: str | None = chr(int(sequence[2:], 16))
        else:
            # ``\u`` without four hex digits (``C:\users``) is a plain backslash.
            char = _ESCAPED_CHARS.get(sequence[1])
        if char is None:
            parts.append(sequence)
            starts.extend(range(escape.start(), escape.end()))
        else:
            parts.append(char)
            starts.append(escape.start())
        pos = escape.end()
    parts.append(text[pos:end])
    starts.extend(range(pos, end))
    starts.append(end)
    return "".join(parts), starts


def _value_spans(text: str, depth: int = 0) -> list[tuple[int, int]]:
    """The secret values of every form, also those inside an escaped string."""
    spans = [
        *_long_option_spans(text),
        *_tool_spans(text),
        *_login_spans(text),
        *_connection_spans(text),
        *_environment_spans(text),
        *_xml_spans(text),
    ]
    if depth < _MAX_STRING_DEPTH:
        for start, end in _escaped_strings(text):
            inner, starts = _decoded(text, start, end)
            spans.extend((starts[a], starts[b]) for a, b in _value_spans(inner, depth + 1))
    return _merge(spans)


def _left_to_next_rule(
    text: str, spans: list[tuple[int, int]], next_rule: re.Pattern[str]
) -> list[tuple[int, int]]:
    """*spans*, without those that *next_rule* hides whole, joined with those it hides in part.

    *next_rule* runs on the same text right after this rule. A value it
    hides whole is left to it, so that part of the answer stays as it was.
    A value it hides only in part is joined with what it hides there, so
    nothing that it hides is shown.
    """
    covers = [match.span() for match in next_rule.finditer(text)]
    if not covers:
        return spans
    ends = [end for _start, end in covers]
    kept: list[tuple[int, int]] = []
    for start, end in spans:
        first = bisect.bisect_right(ends, start)
        last = first
        while last < len(covers) and covers[last][0] < end:
            last += 1
        overlapping = covers[first:last]
        if len(overlapping) == 1 and overlapping[0][0] <= start and end <= overlapping[0][1]:
            continue
        if overlapping:
            start = min(start, overlapping[0][0])
            end = max(end, overlapping[-1][1])
        kept.append((start, end))
    return _merge(kept)


class CommandSecretFinder:
    """Report the secret value of each command line form listed in the module docstring.

    With *next_rule* (the ``api_key`` pattern, which runs right after this
    rule), a value that rule hides whole is left to it, and a value it
    hides in part is joined with that part.
    """

    def __init__(self, next_rule: re.Pattern[str] | None = None) -> None:
        self._next_rule = next_rule

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret value, left to right, never overlapping."""
        spans = _value_spans(text)
        if self._next_rule is None or not spans:
            return spans
        return _left_to_next_rule(text, spans, self._next_rule)


#: The finder on its own: it reports every value it reads.
COMMAND_SECRET: Final[CommandSecretFinder] = CommandSecretFinder()
