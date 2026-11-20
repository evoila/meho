# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find a password that stands on a command line, in text.

Log text, journal text and process lists show commands as they were
typed. The key=value rules do not know the forms that command lines use.
This finder reports only the secret value of each of these forms, so the
option name and the words around it stay readable:

* a long option with a space or ``=`` before the value, where the last
  word of the name is ``password``, ``passwd``, ``pass``, ``pwd``,
  ``passphrase``, ``token`` or ``secret``, or the name ends in ``api-key``;
* short options, only after the tool name in the same command: ``sshpass
  -p``, ``-p`` glued to the value after ``mysql``, ``mysqldump`` or
  ``mariadb`` (not ``-pthread``), ``-P`` after ``sqlcmd``, ``bcp``,
  ``osql`` or ``nsrsqlsv``, ``-w`` after ``ldapsearch`` or ``ldapmodify``,
  the part after the first ``:`` of ``curl -u`` or ``--user``, and the
  part after the first ``%`` of ``smbclient -U``. A tool name in quotes is
  read too: its options follow the closing quote;
* environment style names that end in ``_PASS``, ``_PWD``, ``_PASSWORD``,
  ``_SECRET``, ``_TOKEN`` or ``_KEY``.

Connection strings, a ``password=`` word outside an option, a database
login with user, password and service, the text of an XML element named
like a secret, a quoted value that runs over a line break, and strings
inside JSON strings are not read by this rule yet, see Task #6747.

A value is read the way a shell reads one word: it ends at white space or
at an operator (``; | & < > ( )``) that is not quoted or escaped. Quoted
parts, escaped characters and several parts joined together are one
value. A closing quote counts only on the line of the opening quote. A
quote that is never closed is a plain character inside a value; a value
that starts with one runs to the end of its line. A run of backslashes
that a double quote follows ends the value before the backslashes: in a
JSON string inside a JSON string that is the escaped closing quote. A
form that stands in a quoted command or in a JSON string ends at the
closing quote of that string, so a JSON log line still parses.

A value that an earlier step hid already stays: a ``[REDACTED...]`` mark,
an older mark (``***REDACTED***``, ``<redacted>``) or only the characters a
tool prints in place of a typed password, also written as JSON escapes.

Names that look like a secret but are not one stay: paging tokens
(``--next-token``), names that start with ``no``, a short list of ``_KEY``
names that hold no secret (``S3_KEY``, ``PARTITION_KEY``) and the compiler
option ``-pthread``.

With *leave_to* (the ``api_key`` pattern in :mod:`.patterns`), the finder
leaves a value to that rule, so the answer stays as that rule makes it.
It drops a value when a match of the pattern overlaps it or ends where it
starts, and when a label of the pattern ends where the value starts and a
character other than white space or a quote follows the value. In a
policy without the ``api_key`` rule such a value is not hidden.

Every scan is linear. No pattern has a nested repeat, values are read by
position, a form that starts inside an earlier hidden value is not read
again, and the matches of *leave_to* are found once per text.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["COMMAND_SECRET", "CommandSecretFinder", "merge_spans"]

#: A value that an earlier step hid already: a ``[REDACTED:...]`` mark, an
#: older mark (``[REDACTED]``, ``***REDACTED***``, ``<redacted>``) or a run of
#: the characters a tool prints in place of a typed password (the
#: ``MASK_CHARS`` of :mod:`.patterns`), also in quotes and also written as
#: JSON escapes (``\\u2022``). It is no secret.
_ALREADY_HIDDEN: Final = re.compile(
    r"""(['"]?)(?:\[REDACTED(?::[^\[\]\s'"]+)?\]|\*{3}REDACTED\*{3}|<redacted>"""
    r"""|(?:[*\u2022\u25cf\u00b7]|(?-i:\\u)(?:2022|25cf|00b7))+)\1""",
    re.IGNORECASE,
)

#: A run of bare value characters (no white space, quote, backslash or operator).
_BARE: Final = re.compile(r"""[^\s'"\\;|&<>()]+""")
_DOUBLE_QUOTE_MARK: Final = re.compile(r"""[\\"]""")
_BACKSLASHES: Final = re.compile(r"\\+")


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

    It does only on the line of the opening quote. A quote further down
    belongs to a later log line, so the value is not closed.
    """
    return text.find("\n", opening, after) == -1


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
            run = _BACKSLASHES.match(text, pos, size)
            run_end = size if run is None else run.end()
            if run_end < size and text[run_end] == '"':
                # ``\"`` closes a JSON string inside a JSON string.
                return pos
            # Pairs are plain backslashes; an odd one takes the next character.
            pos = min(run_end + (run_end - pos) % 2, size)
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
    """True when *value* is a secret to hide: not empty and not hidden already."""
    return bool(value) and _ALREADY_HIDDEN.fullmatch(value) is None


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


# -- the finder -------------------------------------------------------------------


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort the spans and join those that touch or overlap."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


#: How much text before a value is read to find a label of *leave_to*, and
#: the letters put after it in place of a value the label would take.
_LABEL_WINDOW: Final = 80
_PROBE: Final = "aaaaaaaa"


def _after_label(text: str, start: int, leave_to: re.Pattern[str]) -> bool:
    """True when a label of *leave_to* ends at *start* (``--password=``)."""
    begin = max(0, start - _LABEL_WINDOW)
    probe = text[begin:start] + _PROBE
    cut = start - begin
    return any(m.end() == len(probe) and m.start() < cut for m in leave_to.finditer(probe))


def _left_to(
    text: str, spans: list[tuple[int, int]], leave_to: re.Pattern[str]
) -> list[tuple[int, int]]:
    """The sorted *spans* that *leave_to* does not hide on its own.

    The matches do not overlap, so the first match that ends at or after
    the start of a value is the only one to check. Both lists are read
    forward once.
    """
    matches = [m.span() for m in leave_to.finditer(text)]
    kept: list[tuple[int, int]] = []
    at = 0
    for start, end in spans:
        while at < len(matches) and matches[at][1] < start:
            at += 1
        if at < len(matches) and matches[at][0] < end:
            continue
        follows = text[end : end + 1]
        glued = bool(follows) and not follows.isspace() and follows not in "'\""
        if glued and _after_label(text, start, leave_to):
            continue
        kept.append((start, end))
    return kept


def _value_spans(text: str) -> list[tuple[int, int]]:
    """The secret values of every form, sorted and merged."""
    return merge_spans([*_long_option_spans(text), *_tool_spans(text), *_environment_spans(text)])


class CommandSecretFinder:
    """Report the secret value of each command line form listed in the module docstring.

    With *leave_to*, a value that pattern hides is left to it (see the
    module docstring).
    """

    def __init__(self, leave_to: re.Pattern[str] | None = None) -> None:
        self._leave_to = leave_to

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret value, left to right, never overlapping."""
        spans = _value_spans(text)
        if self._leave_to is None or not spans:
            return spans
        return _left_to(text, spans, self._leave_to)


#: The finder on its own: it reports every value it reads.
COMMAND_SECRET: Final[CommandSecretFinder] = CommandSecretFinder()
