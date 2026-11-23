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
  read too: its options follow the closing quote. A tool name is read only
  when it names the program that runs: at the start of a command, also
  with a path (``/opt/tools/mysql``), after a wrapper that runs it
  (``sudo``, ``nice -n 5``, ``timeout 5``, ``chroot /x``, ``xargs``), or
  after ``-exec`` of ``find``. The words of a command are split the way a
  shell splits them, and the command is the first word after any wrapper.
  A tool name is not read right after an option, whose value it is
  (``docker run --name "mysql"``), or as an argument of a command that
  takes files or names (``sudo find mysql``, ``find "a b" mysql``);
* environment style names that end in ``_PASS``, ``_PWD``, ``_PASSWORD``,
  ``_SECRET``, ``_TOKEN`` or ``_KEY``.

Each JSON string of the text is decoded and read like plain text, also a
JSON string inside another one, up to three levels deep. So the forms are
hidden there too. A value found there ends at the end of its string, and
the mark replaces whole escapes, so every JSON level still parses.

Not read by this rule yet, see Task #6747: connection strings, a
``password=`` word outside an option, a database login with user,
password and service, the text of an XML element named like a secret, a
single-quoted command that is joined with an escaped single quote before
the option, a quoted command or value that runs over a line break, tool
names in capitals or with ``.EXE``, and the other forms that Task lists.

A value is read the way a shell reads one word: it ends at white space or
at an operator (``; | & < > ( )``) that is not quoted or escaped. Quoted
parts, escaped characters and several parts joined together are one
value. A part that starts with ``$'`` is ANSI-C quoted: a backslash takes
the next character there, also a single quote. A closing quote counts
only on the line of the opening quote. A quote that is never closed is a
plain character inside a value. When a value starts with one, its end is
not known, and the rest of its command is hidden, up to the next ``;``,
``|``, ``&`` or line end. An empty value (``''``) is no secret. In plain text an
escaped double quote belongs to the value, as in a shell word. Only
inside a JSON string or a double-quoted command does a run of
backslashes that a double quote follows end the value, before the
backslashes: in a JSON string inside a JSON string that is the escaped
closing quote. There an escaped line end or tab (backslashes before
``n``, ``r`` or ``t``) also ends the value, before the backslashes, and
an escaped line end ends the command of a tool, as a real line end does.
The same holds for a ``\\u`` escape of these characters (``\\u000d``), and
a ``\\u`` escape of ``;``, ``|`` or ``&`` ends the value and the command
as the operator does.
Only a run that is a line end at the escape level of the string that holds
the tool ends the command; a written backslash before ``n`` or ``r``, as
in a Windows path, does not. A
form that stands in a quoted command or in a JSON string, also a
double-quoted string inside a single-quoted command, ends at the closing
quote of that string, so a JSON log line still parses.

A double-quoted string that is a shell word (white space and a word stand
before its opening quote, as in ``sh -c "..."``) is also read the way a
shell reads it: a backslash is dropped only before ``"``, a backslash,
``$`` and a backtick, so ``\\n`` and ``\\u0026`` are text there. The value
the shell passes is hidden up to the closing quote. This runs on the text
and on every decoded JSON level, and only adds to the values found above.

A value that an earlier step hid already stays: a ``[REDACTED...]`` mark,
an older mark (``***REDACTED***``, ``<redacted>``) or only the characters a
tool prints in place of a typed password, also written as JSON escapes.

Names that look like a secret but are not one stay: paging tokens
(``--next-token``), names that start with ``no``, ``ask`` or ``skip``
(``--ask-pass`` takes no value), a short list of ``_KEY``
names that hold no secret (``S3_KEY``, ``PARTITION_KEY``) and the compiler
option ``-pthread``.

With *leave_to* (the ``api_key`` pattern in :mod:`.patterns`), the finder
leaves a value to that rule, so the answer stays as that rule makes it.
It drops a value when a match of the pattern overlaps it or ends where it
starts, and when a label of the pattern ends where the value starts and a
character other than white space or a quote follows the value. The
policies run this rule first, and the pattern would read a mark with such
a character after it as a value and hide the label too. When the value
has quotes, the finder hides only the text of each quoted or bare part
instead (``'a b'" c"``), so the label, the quotes and the next command
stay. A second rule, ``command_line_secret_rest`` (the finder with
*rest*), runs after the ``api_key`` rule and hides whole every value that
is still readable. So a value is left to the ``api_key`` rule only when
that rule hides at least a part of it. A value of only marks, quotes and
backslashes is no secret, so the second rule keeps the work of the first.

Every scan is linear. No pattern has a nested repeat, values are read by
position, a form that starts inside an earlier hidden value is not read
again, a run of backslashes is passed in one step (also when a value is
split into its quoted parts), each JSON string and
each shell word is decoded once per level, and the matches of
*leave_to* are found once per text.
"""

from __future__ import annotations

import bisect
import collections
import functools
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
#: Backslashes before ``n``, ``r`` or a ``u`` escape of a line end, ``;``, ``|``
#: or ``&`` inside a JSON string: maybe an escaped end of a command.
_ESCAPED_COMMAND_END: Final = re.compile(r"(\\+)(?:[nr]|u00(?:0[aAdD]|3[bB]|7[cC]|26))")
#: The ``u`` escape of a character that ends a word in a JSON string: a tab,
#: a line end, a double quote or an operator (``\\u000d``, ``\\u0026``).
_ESCAPED_WORD_END: Final = re.compile(r"u00(?:0[9aAdD]|2[2689]|3[bBcCeE]|7[cC])")


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


def _value_end(text: str, start: int, size: int | None = None, *, in_string: bool = False) -> int:
    """Index where the shell word that starts at *start* ends (``start`` when empty).

    *size* is where the reading stops, as the closing quote of a command
    that is quoted inside a longer command (``sh -c "mysql -px"``).
    *in_string* is true when the word stands inside a double-quoted string
    (a JSON string or a quoted command). Only there does ``\\"`` end the
    word, and so do ``\\n``, ``\\r`` and ``\\t`` (an escaped line end or
    tab) and a ``\\u`` escape of one of these or of an operator; in plain
    text they are escaped characters and part of the word.
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
            if (
                in_string
                and run_end < size
                and (text[run_end] in '"nrt' or _ESCAPED_WORD_END.match(text, run_end, size))
            ):
                # ``\"`` closes a JSON string inside a JSON string; ``\n``,
                # ``\r``, ``\t`` and the ``\u`` escapes are a line end, a tab,
                # a quote or an operator in a JSON string.
                return pos
            # Pairs are plain backslashes; an odd one takes the next character.
            pos = min(run_end + (run_end - pos) % 2, size)
            continue
        if char in "'\"":
            closing = _closing_of(text, pos, size)
            if closing > 0 and _closes_value(text, pos, closing):
                pos = closing
                continue
            if pos > start:
                # A quote inside a word that is never closed is a plain character.
                pos += 1
                continue
            # The end of the value is not known: hide the rest of the command.
            command_end = _COMMAND_END.search(text, pos, size)
            return size if command_end is None else command_end.start()
        break
    return pos


#: What ends a command: a line end or an operator that joins commands.
_COMMAND_END: Final = re.compile(r"[\n;|&]")
#: A single quote or a backslash, read inside an ANSI-C quoted part (``$'...'``).
_ANSI_MARK: Final = re.compile(r"['\\]")


def _is_ansi_quote(text: str, quote: int) -> bool:
    """True when the single quote at *quote* opens an ANSI-C quoted part (``$'a\\'b'``)."""
    return text[quote] == "'" and quote > 0 and text[quote - 1] == "$"


def _closing_of(text: str, opening: int, size: int) -> int:
    """Index after the quote that closes the one at *opening*, or ``0`` when there is none.

    In an ANSI-C quoted part a backslash takes the next character, also a
    single quote.
    """
    if text[opening] == '"':
        return max(_skip_double_quoted(text, opening, size), 0)
    if not _is_ansi_quote(text, opening):
        return text.find("'", opening + 1, size) + 1
    pos = opening + 1
    while (mark := _ANSI_MARK.search(text, pos, size)) is not None:
        if mark.group(0) == "'":
            return mark.end()
        pos = mark.start() + 2
    return 0


#: The quotes and backslashes around and between hidden parts of a value.
_NO_SECRET: Final = str.maketrans("", "", "'\"\\$")


def _is_hidden(value: str) -> bool:
    """True when *value* is a secret to hide.

    An empty value (also ``''`` and ``""``) is no secret, and neither is a
    value that holds only marks of an earlier step, with quotes or
    backslashes around them.
    """
    return bool(_ALREADY_HIDDEN.sub("", value).translate(_NO_SECRET))


def _value_span(
    text: str, start: int, size: int | None = None, *, in_string: bool = False
) -> tuple[int, int] | None:
    """The span of the shell word at *start*, or ``None`` when it holds no secret."""
    end = _value_end(text, start, size, in_string=in_string)
    return (start, end) if _is_hidden(text[start:end]) else None


def _after(
    text: str, start: int, mark: str, size: int | None = None, *, in_string: bool = False
) -> tuple[int, int] | None:
    """The part of the shell word at *start* behind the first *mark* (``user:value``)."""
    end = _value_end(text, start, size, in_string=in_string)
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

#: The states of :class:`_OuterQuotes`: outside, in a single-quoted string,
#: in a double-quoted string, and in a double-quoted string inside a
#: single-quoted one.
_SINGLE_DOUBLE: Final = "'\""
_MARKS: Final[dict[str, re.Pattern[str]]] = {
    "": re.compile(r"""['"\\\n]"""),
    "'": re.compile(r"""['"\n]"""),
    '"': re.compile(r'["\\\n]'),
    _SINGLE_DOUBLE: re.compile(r"""['"\\\n]"""),
}
#: The state after a quote mark; a line end, and every pair not listed, ends all strings.
_NEXT_STATE: Final[dict[tuple[str, str], str]] = {
    ("", "'"): "'",
    ("", '"'): '"',
    ("'", '"'): _SINGLE_DOUBLE,
    (_SINGLE_DOUBLE, '"'): "'",
}
#: A quote mark or a backslash, read inside a double-quoted string.
_QUOTE_OR_ESCAPE: Final = re.compile(r"""['"\\]""")


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
        self._single = -1
        self._single_line = -1

    def _end_of_line(self, index: int) -> int:
        if self._line_end < index:
            line_end = self._text.find("\n", index)
            self._line_end = len(self._text) if line_end == -1 else line_end
        return self._line_end

    def _next_single(self, index: int, line_end: int) -> int:
        """The first single quote from *index* on its line, or ``-1``.

        The answer is kept while it lies ahead, so several inner strings of
        one single-quoted string do not read its rest again.
        """
        if self._single_line != line_end or -1 < self._single < index:
            self._single = self._text.find("'", index, line_end)
            self._single_line = line_end
        return self._single

    def _advance(self, index: int) -> None:
        text = self._text
        while self._pos < index:
            mark = _MARKS[self._quote].search(text, self._pos, index)
            if mark is None:
                self._pos = index
                return
            char = mark.group(0)
            if char == "\\":
                self._pos = mark.start() + 2
                continue
            self._pos = mark.end()
            self._quote = _NEXT_STATE.get((self._quote, char), "")
            self._close = None

    def end(self, index: int) -> int | None:
        """The closing quote of the string that *index* stands in, or ``None``."""
        self._advance(index)
        if not self._quote:
            return None
        if self._close is None:
            text = self._text
            line_end = self._end_of_line(index)
            close = -1
            if self._quote == _SINGLE_DOUBLE:
                close = _closes_inner(text, index, line_end)
            if close == -1 and self._quote != '"':
                close = _closing(text, self._next_single(index, line_end))
            elif close == -1:
                after = _skip_double_quoted(text, index - 1, line_end)
                close = _closing(text, after - 1 if after > 0 else -1)
            self._close = close
        return self._close if self._close >= 0 else None


def _closing(text: str, close: int) -> int:
    """*close*, or ``-1`` when it is missing or a word stands right behind it.

    A quote with a word right behind it opens a quoted value (``don't
    --password 'a b'``): it does not close a string.
    """
    return close if close >= 0 and _ends_string(text, close + 1) else -1


def _closes_inner(text: str, index: int, line_end: int) -> int:
    """The double quote that closes a double-quoted string inside a single-quoted one.

    A single quote before it ends both strings, so then there is none (``-1``).
    """
    pos = index
    while (mark := _QUOTE_OR_ESCAPE.search(text, pos, line_end)) is not None:
        if mark.group(0) == "\\":
            pos = mark.start() + 2
            continue
        return _closing(text, mark.start()) if mark.group(0) == '"' else -1
    return -1


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


def _in_double_quotes(text: str, close: int | None) -> bool:
    """True when the closing quote that :class:`_OuterQuotes` found is a double quote."""
    return close is not None and text[close] == '"'


def _name_words(name: str) -> list[str]:
    return [word for word in _NAME_SPLIT.split(_CAMEL.sub("_", name).lower()) if word]


def _is_paging_token(words: list[str]) -> bool:
    """True for ``next token``, ``continuation token`` and the like."""
    return len(words) > 1 and words[-1] == "token" and words[-2] in _PAGING_WORDS


def _is_secret_option(name: str) -> bool:
    words = _name_words(name)
    if not words or words[0] in ("no", "ask", "skip") or _is_paging_token(words):
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
        close = quotes.end(match.start())
        span = _value_span(text, match.end(), close, in_string=_in_double_quotes(text, close))
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


def _is_program_folder(path: str) -> bool:
    """True when *path*, the part of a word before a tool name, is a folder of programs.

    A folder named ``bin`` or ``sbin`` (``/usr/bin/``) or the current or
    parent folder (``./``) holds the program. A tool name at the end of
    another path (``/tmp/mysql``) names a file or folder, not the tool.
    """
    folder = path.replace("\\", "").rstrip("/").rsplit("/", 1)[-1]
    return folder in ("", ".", "..", "~") or folder.endswith("bin")


def _is_option(word: str) -> bool:
    """True for an option that may take the next word as its value (``--name``, ``-n``)."""
    return word.startswith("-") and word != "--" and "=" not in word


def _tool_name(word: str, *, any_folder: bool = False) -> str | None:
    """The tool a command word names, with its family folded in, or ``None``.

    With *any_folder* the word is known to be the program that runs, so a
    path outside a folder of programs counts too (``/opt/tools/mysql``).
    """
    folder, slash, base = word.rpartition("/")
    if slash and not any_folder and not _is_program_folder(folder + slash):
        return None
    match = _TOOL.fullmatch(base)
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


def _read(
    text: str, begin: int | None, mark: str | None, size: int, in_string: bool
) -> tuple[int, int] | None:
    """The secret span of the value at *begin*: all of it, or what follows *mark*."""
    if begin is None:
        return None
    if mark is None:
        return _value_span(text, begin, size, in_string=in_string)
    return _after(text, begin, mark, size, in_string=in_string)


def _option_span(
    text: str, tools: set[str], word: str, span: tuple[int, int], size: int, in_string: bool
) -> tuple[int, int] | None:
    """The secret value of one short option of the tools of this command."""
    start, end = span

    def value(flags: tuple[str, ...], mark: str | None = None) -> tuple[int, int] | None:
        begin = _value_start(text, word, start, end, size, flags)
        return _read(text, begin, mark, size, in_string)

    if "sshpass" in tools and word.startswith("-p"):
        tools.discard("sshpass")
        return value(("-p",))
    if "mysql" in tools and word.startswith("-p") and len(word) > 2 and word != "-pthread":
        return _read(text, start + 2, None, size, in_string)
    if tools & _UPPER_P_TOOLS and word.startswith("-P"):
        return value(("-P",))
    if "ldap" in tools and word.startswith("-w"):
        return value(("-w",))
    if "curl" in tools and word.startswith(("-u", "--user")):
        return value(("-u", "--user"), ":")
    if "smbclient" in tools and word.startswith("-U"):
        return value(("-U",), "%")
    return None


def _is_escaped_command_end(run: re.Match[str], unit: int) -> bool:
    """True when the backslashes of *run* write an escape, not a backslash.

    The escape is a line end, or a ``\\u`` escape of a line end, ``;``,
    ``|`` or ``&``. *unit* is how many backslashes a line end takes at the escape level of
    the string the tool stands in: one in a JSON string, two in a JSON
    string inside a JSON string. A written backslash doubles that count,
    so a run is a line end only when its lowest set bit is at most *unit*
    (``C:\\\\new`` in a JSON string is a path, ``x\\ny`` is a line end).
    """
    count = len(run.group(1))
    return count & -count <= unit


def _segment_spans(
    text: str, first: int, tool: str, size: int, in_string: bool, unit: int = 1
) -> tuple[list[tuple[int, int]], int]:
    """Read one command from the tool that ends at *first*; return spans and the end.

    *unit* is the number of backslashes of an escaped line end in the
    string that holds the command (see :func:`_is_escaped_command_end`).
    """
    spans: list[tuple[int, int]] = []
    tools = {tool}
    pos = first
    skip_next = False
    previous = ""
    # The word after the password of ``sshpass`` is the program it runs.
    runs_next = False
    while pos < size:
        gap = _BLANKS.match(text, pos, size)
        pos = pos if gap is None else gap.end()
        run = _ESCAPED_COMMAND_END.match(text, pos, size) if in_string else None
        if (
            pos >= size
            or text[pos] in "\n;|&"
            or (run is not None and _is_escaped_command_end(run, unit))
        ):
            break
        # A whole shell word, so a ``;`` or a space inside quotes does not end it.
        # Written backslashes before ``n`` or ``r`` (a path) belong to the word.
        start = pos
        end = _value_end(text, pos if run is None else run.end(), size, in_string=in_string)
        if end == start and text[start] == "\\":
            # An escaped tab or quote follows the run: pass the whole run at
            # once, so a long run is read in linear time.
            backslashes = _BACKSLASHES.match(text, start, size)
            end = start + 1 if backslashes is None else backslashes.end()
        elif end == start:
            # ``< > ( )`` is a word of its own.
            end += 1
        token = text[start:end]
        pos = end
        option_before, previous = _is_option(previous), token
        program, runs_next = runs_next, False
        if skip_next:
            skip_next = False
            continue
        sshpass = "sshpass" in tools
        span = _option_span(text, tools, token, (start, end), size, in_string)
        if span is not None:
            spans.append(span)
            pos = max(pos, span[1])
            # The option took its value, so the next word is no option value.
            previous = ""
            runs_next = sshpass and "sshpass" not in tools
        elif "sshpass" in tools and token in _SSHPASS_VALUE_OPTIONS:
            skip_next = True
        elif not option_before and (name := _tool_name(token, any_folder=program)) is not None:
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


_QUOTE_OR_LINE_END: Final = re.compile(r'["\n]')


class _OpenStrings:
    """The escape level of the innermost open JSON string at a position, read left to right.

    A double quote with *b* backslashes before it belongs to escape level
    one more than the count of the low one bits of *b*: no backslash (or
    an even run) in a JSON string, one in a JSON string inside a JSON
    string, three one level deeper. A quote opens a string of its level
    or closes the open one, and closing a string also closes every string
    inside it. So a quoted word that is opened and closed before the tool,
    as ``\\"ok\\"``, does not change the level. A real line end closes all
    strings. Each part of the text is read once.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._seen = 0
        self._open: list[int] = []

    def unit(self, index: int) -> int:
        """Backslashes of an escaped line end in the innermost string open at *index*."""
        text, levels = self._text, self._open
        if index > self._seen:
            for mark in _QUOTE_OR_LINE_END.finditer(text, self._seen, index):
                quote = mark.start()
                if text[quote] == "\n":
                    levels.clear()
                    continue
                run_start = quote
                while run_start > 0 and text[run_start - 1] == "\\":
                    run_start -= 1
                after = quote - run_start + 1
                level = (after & -after).bit_length()
                while levels and levels[-1] > level:
                    levels.pop()
                if levels and levels[-1] == level:
                    levels.pop()
                else:
                    levels.append(level)
            self._seen = index
        return 1 << (levels[-1] - 1) if levels else 1


#: The characters that end a word when it is read back from a tool name.
_WORD_EDGE: Final = frozenset(" \t\n\r'\"`;|&<>(){}[],=")
#: How far back a path or the words before a tool name are read.
_LOOK_BACK: Final = 256
#: The characters that end a command: a word after one starts a new command.
_COMMAND_EDGE: Final = frozenset("\n\r;|&(){}`")
#: Commands that run the command that follows them, each with its options
#: that take the next word as their value (``sudo -u root mysql``). Shell
#: words that start a command (``then``, ``do``) are here too.
_WRAPPERS: Final[dict[str, frozenset[str]]] = {
    "sudo": frozenset(
        {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"}
        | {"--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type"}
        | {"--other-user", "--close-from", "--command-timeout"}
    ),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "xargs": frozenset(
        {"-a", "-d", "-E", "-e", "-I", "-L", "-l", "-n", "-P", "-s"}
        | {"--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars"}
    ),
    "strace": frozenset({"-o", "-p", "-e", "-s", "-u", "-a", "-E", "-P"}),
    "watch": frozenset({"-n", "--interval"}),
    "runuser": frozenset({"-u", "-g", "-G", "--user", "--group"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "chroot": frozenset({"--userspec", "--groups"}),
    "nohup": frozenset(),
    "exec": frozenset({"-a"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "setsid": frozenset(),
    "then": frozenset(),
    "do": frozenset(),
    "else": frozenset(),
}
#: Wrappers that take fixed words before the command they run: the time of
#: ``timeout`` and the folder of ``chroot``.
_WRAPPER_WORDS: Final[dict[str, int]] = {"timeout": 1, "chroot": 1}
#: The options of ``find`` whose next word is a program it runs.
_FIND_RUNS: Final = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: Container tools whose ``exec`` or ``run`` takes a container or image name next.
_CONTAINER_TOOLS: Final = frozenset({"docker", "podman", "nerdctl", "kubectl", "oc", "crictl"})
#: Commands whose plain words are files, folders or names, not a program to
#: run (``find mysql -print``). After any other command a tool name is read
#: as the program, because a hidden value is safer than a shown one.
_ARGUMENT_COMMANDS: Final = frozenset(
    {"find", "ls", "cat", "cd", "rm", "cp", "mv", "ln", "du", "df", "stat", "file"}
    | {"tail", "head", "less", "more", "grep", "egrep", "fgrep", "echo", "printf"}
    | {"chown", "chmod", "chgrp", "mkdir", "rmdir", "touch", "tar", "zip", "unzip"}
    | {"rsync", "scp", "ping", "which", "whereis", "type", "man", "kill", "pkill"}
    | {"pgrep", "killall", "pidof", "locate", "wc", "sort", "diff", "readlink"}
    | {"realpath", "basename", "dirname", "tree", "systemctl", "service"}
)
#: A variable set for one command (``LANG=C mysql``).
_ASSIGNMENT: Final = re.compile(r"[A-Za-z_]\w*=\S*")


def _word_back(text: str, end: int) -> str:
    """The word that ends at *end*, read back to white space, a quote or an operator."""
    begin = end
    while begin > max(0, end - _LOOK_BACK) and text[begin - 1] not in _WORD_EDGE:
        begin -= 1
    return text[begin:end]


def _partner(chunk: str, quote: int) -> int:
    """The index of the quote that closes the one at *quote* in *chunk*, or ``-1``.

    It is the next quote of the same kind with as many backslashes before
    it, so ``\\"`` in a JSON string pairs only with ``\\"``.
    """
    level = 0
    while quote - level > 0 and chunk[quote - level - 1] == "\\":
        level += 1
    pos = quote + 1
    while (close := chunk.find(chunk[quote], pos)) != -1:
        run = 0
        while close - run > quote + 1 and chunk[close - run - 1] == "\\":
            run += 1
        if run == level:
            return close
        pos = close + 1
    return -1


def _shell_words(chunk: str) -> tuple[list[str], bool]:
    """The words of *chunk*, split the way a shell splits them.

    A quoted part with spaces belongs to its word (``"some folder"``). A
    quote that is not closed in *chunk* opens a quoted command, which
    starts after it (``sh -c 'sudo``); then the second value is true.
    """
    words: list[str] = []
    word: list[str] = []
    restarted = False
    pos = 0
    while pos < len(chunk):
        char = chunk[pos]
        if char.isspace():
            if word:
                words.append("".join(word))
                word = []
            pos += 1
            continue
        if char in "'\"":
            close = _partner(chunk, pos)
            if close == -1:
                # The opening quote of a quoted command: the command starts after it.
                words, word, restarted = [], [], True
                pos += 1
                continue
            word.append(chunk[pos : close + 1])
            pos = close + 1
            continue
        word.append(char)
        pos += 1
    if word:
        words.append("".join(word))
    return words, restarted


def _words_before(text: str, end: int) -> tuple[list[str], bool]:
    """The words of the command before *end*, first word first.

    The reading stops at a character that ends a command, at the start of
    a line and at the opening quote of a quoted command (``sh -c 'sudo``).
    The second value is true when the first word starts the command.
    """
    limit = max(0, end - _LOOK_BACK)
    chunk = text[limit:end]
    edge = max(chunk.rfind(char) for char in _COMMAND_EDGE)
    words, restarted = _shell_words(chunk[edge + 1 :])
    return words, restarted or edge != -1 or limit == 0


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _hands_on(words: list[str]) -> bool:
    """True when a command in *words* runs the word after them (``sudo -n``, ``nice -n 5``)."""
    for at, word in enumerate(words):
        if at and word == "exec" and _base(words[at - 1]) in _CONTAINER_TOOLS:
            continue
        if _base(word) in _WRAPPERS and _command_at(words, at) == len(words):
            return True
    return False


def _command_at(words: list[str], at: int = 0) -> int:
    """The index of the program that the command at *at* runs, after any wrapper.

    A wrapper is skipped with its options, the values of those options and
    its fixed words (``timeout 5``), and so is a variable set for the
    command (``LANG=C``). ``len(words)`` when the next word is the program,
    more when it is still the value of an option or a fixed word.
    """
    while at < len(words):
        word = words[at]
        options = _WRAPPERS.get(_base(word))
        if options is None and not _ASSIGNMENT.fullmatch(word):
            return at
        at += 1
        if options is None:
            continue
        while at < len(words) and (words[at].startswith("-") or _ASSIGNMENT.fullmatch(words[at])):
            at += 2 if words[at] in options else 1
        at += _WRAPPER_WORDS.get(_base(word), 0)
    return at


def _is_command_position(words: list[str], at_start: bool) -> bool:
    """True when a word after *words* is the program that runs, not an argument.

    It is after a command that runs the next word (``sudo -n``), after
    ``--``, after a variable set for the command (``LANG=C``) and after
    ``-exec`` of ``find``. It is not the value or argument after an option
    (``docker run --name``), the container after ``docker exec``, or an
    argument of a command that takes files or names (``sudo find``).
    After other words (another command, the columns of a process list, a
    log prefix) it is the program.
    """
    if not at_start:
        return _after_words(words)
    command = _command_at(words)
    if command >= len(words):
        return command == len(words)
    if _base(words[command]) == "find":
        runs = [at for at in range(command, len(words)) if words[at] in _FIND_RUNS]
        if runs:
            return _is_command_position(words[runs[-1] + 1 :], True)
    if not _after_words(words[command:]):
        return False
    return _base(words[command]) not in _ARGUMENT_COMMANDS


def _after_words(words: list[str]) -> bool:
    """True when the word after *words* may be a program, when the command is not known."""
    if not words or _hands_on(words):
        return True
    last = words[-1]
    if last == "--":
        return True
    if last.startswith("-"):
        return False
    if last in ("exec", "run") and len(words) > 1 and _base(words[-2]) in _CONTAINER_TOOLS:
        return False
    if _ASSIGNMENT.fullmatch(last):
        return len(words) == 1 or not words[-2].startswith("-")
    return True


def _runs_tool(text: str, match: re.Match[str]) -> bool:
    """True when the tool name at *match* is the program that runs, not an argument.

    A path before the name (``/opt/tools/mysql``) and quotes around it
    (``"mysql"``) belong to the word. A quote that opens before the name and
    does not close right after it starts a quoted command (``sh -c "mysql``).
    After ``=`` the word is the value of an option (``--name=mysql``), or
    of a name that holds a command (``COMMAND=/usr/bin/mysql``).
    """
    begin = match.start() - len(_word_back(text, match.start()))
    before = text[begin - 1] if begin else ""
    if before in ("'", '"'):
        lead = begin - 1
        while lead > 0 and text[lead - 1] == "\\":
            lead -= 1
        if not text.startswith(text[lead:begin], match.end()):
            return True
        begin = lead
        before = text[begin - 1] if begin else ""
    if before == "=":
        return not _word_back(text, begin - 1).startswith("-")
    if before not in (" ", "\t"):
        return True
    return _is_command_position(*_words_before(text, begin))


def _tool_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quotes = _OuterQuotes(text)
    open_strings = _OpenStrings(text)
    pos = 0
    while (match := _TOOL.search(text, pos)) is not None:
        if not _runs_tool(text, match):
            pos = match.end()
            continue
        tool = _tool_name(match.group(0)) or ""
        close = quotes.end(match.start())
        first, size = match.end(), len(text) if close is None else close
        in_string = _in_double_quotes(text, close)
        if close is not None and _is_quoted_executable(text, match, close):
            # The arguments follow the closing quote of the tool name, up to
            # the closing quote of a single-quoted command around it.
            outer = quotes.end(close + 1)
            first, size = close + 1, len(text) if outer is None else outer
            in_string = _in_double_quotes(text, outer)
        unit = open_strings.unit(match.start())
        found, pos = _segment_spans(text, first, tool, size, in_string, unit)
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
        close = quotes.end(match.start())
        span = _value_span(text, match.end(), close, in_string=_in_double_quotes(text, close))
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


#: A quote or a backslash: where a part of a value may open.
_QUOTE_OR_BACKSLASH: Final = re.compile(r"""['"\\]""")


def _next_opening(text: str, pos: int, end: int) -> tuple[int, int] | None:
    """The next quote that opens a part of a value, with the backslashes before it.

    Returns where the backslashes start and the index after the quote
    (``\\"`` is the opening quote of a JSON escape). Each run of
    backslashes is read once, so the scan is linear.
    """
    while (mark := _QUOTE_OR_BACKSLASH.search(text, pos, end)) is not None:
        if mark.group(0) != "\\":
            return mark.start(), mark.end()
        run = _BACKSLASHES.match(text, mark.start(), end)
        run_end = end if run is None else run.end()
        if run_end < end and text[run_end] in "'\"":
            return mark.start(), run_end + 1
        pos = run_end
    return None


#: The mark the policies write in place of a value of this rule.
_OWN_MARK: Final = "[REDACTED:command_line_secret]"


def _closing_quote(text: str, opening: tuple[int, int], end: int) -> tuple[int, int]:
    """Where the part that *opening* starts ends: its text end and the index after its quote.

    A single quote closes at the next one. A double quote closes at the
    next one that is not escaped; when the opening quote is a JSON escape
    (``\\"``), at the next one that is escaped the same way, whose
    backslashes stay outside the part.
    """
    begin, after = opening
    quote = text[after - 1]
    if quote == "'" and _is_ansi_quote(text, after - 1) and after - begin == 1:
        close = _closing_of(text, after - 1, end)
        return (end, end) if close == 0 else (close - 1, close)
    if quote == "'":
        close = text.find("'", after, end)
        return (end, end) if close == -1 else (close, close + 1)
    escaped = after - begin > 1
    pos = after
    while (close := text.find('"', pos, end)) != -1:
        run = close
        while run > after and text[run - 1] == "\\":
            run -= 1
        if (close - run) % 2 == escaped:
            return (run if escaped else close, close + 1)
        pos = close + 1
    return (end, end)


def _value_parts(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """The parts of a value with a quote in it, each without its quotes (``'a b'"c"``).

    A bare part between quoted parts is a part too. A value without a
    quote gives no parts.
    """
    parts: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        opening = _next_opening(text, pos, end)
        if opening is None:
            parts.append((pos, end))
            break
        if opening[0] > pos:
            parts.append((pos, opening[0]))
        part_end, pos = _closing_quote(text, opening, end)
        parts.append((opening[1], part_end))
    if len(parts) == 1 and parts[0] == (start, end):
        return []
    return [(begin, stop) for begin, stop in parts if begin < stop]


def _inside_quotes(
    text: str, start: int, end: int, leave_to: re.Pattern[str]
) -> list[tuple[int, int]]:
    """The parts of a quoted value, when *leave_to* then keeps the label.

    The policies run this rule before *leave_to*. A mark that a character
    such as ``;`` follows is read by *leave_to* as a value, and it would
    hide the label too. A mark inside quotes is not, so the quotes stay and
    only the text of each part is hidden. Several parts joined into one
    value (``'a b'" c"``) are each hidden. An empty list means the value
    is left to *leave_to*.
    """
    parts = _value_parts(text, start, end)
    if not parts:
        return []
    begin = max(0, start - _LABEL_WINDOW)
    pieces = [text[begin : parts[0][0]]]
    followers = [part[0] for part in parts[1:]] + [end + _LABEL_WINDOW]
    for (_, part_end), following in zip(parts, followers, strict=True):
        pieces += [_OWN_MARK, text[part_end:following]]
    cut = parts[0][0] - begin
    if any(m.start() < cut < m.end() for m in leave_to.finditer("".join(pieces))):
        return []
    return parts


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
            kept.extend(_inside_quotes(text, start, end, leave_to))
            continue
        kept.append((start, end))
    return kept


# -- JSON strings -----------------------------------------------------------------

_JSON_ESCAPES: Final[dict[str, str]] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    # A carriage return ends a command like a line end, so it is read as one.
    "r": "\n",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}
_HEX4: Final = re.compile(r"[0-9A-Fa-f]{4}")
#: What ends a run of plain characters in a JSON string.
_STRING_MARK: Final = re.compile(r'["\\\n\r]')
#: How many JSON strings inside each other are decoded.
_JSON_LEVELS: Final = 3


def _decode_json_strings(text: str) -> tuple[str, list[int], list[int]]:
    """The JSON strings of *text*, decoded, each followed by a line end.

    A double quote opens a string and the next unescaped double quote
    closes it. An invalid escape or a raw line end means there is no
    string here; the scan goes on after it, so each character is read
    once. Returns the decoded text, the place in *text* of each of its
    characters (the closing quote for each added line end, and the start
    of the escape for a decoded escape), and the places of the added line
    ends in the decoded text.
    """
    parts: list[str] = []
    places: list[int] = []
    ends: list[int] = []
    pos = 0
    while (opening := text.find('"', pos)) != -1:
        # A string that turns out not to be one is taken off the lists again.
        kept_parts, kept_places = len(parts), len(places)
        pos = opening + 1
        while (mark := _STRING_MARK.search(text, pos)) is not None:
            at = mark.start()
            if at > pos:
                parts.append(text[pos:at])
                places.extend(range(pos, at))
            pos = at + 1
            if text[at] != "\\":
                break
            escape = text[at + 1 : at + 2]
            if escape in _JSON_ESCAPES:
                parts.append(_JSON_ESCAPES[escape])
                pos = at + 2
            elif escape == "u" and _HEX4.fullmatch(text, at + 2, at + 6):
                char = chr(int(text[at + 2 : at + 6], 16))
                parts.append("\n" if char == "\r" else char)
                pos = at + 6
            else:
                break
            places.append(at)
        if mark is not None and text[mark.start()] == '"':
            ends.append(len(places))
            parts.append("\n")
            places.append(mark.start())
            continue
        del parts[kept_parts:], places[kept_places:]
        if mark is None:
            break
    return "".join(parts), places, ends


#: What ends a run of plain characters in a double-quoted shell word.
_SHELL_MARK: Final = re.compile(r'["\\\n\r]')
#: The characters a backslash escapes in a double-quoted shell word.
_SHELL_ESCAPED: Final = frozenset('"\\$`')


def _is_shell_word(text: str, opening: int) -> bool:
    """True when the double quote at *opening* starts a shell word (``sh -c "``).

    White space stands before the quote, and a word before that. A string
    at the start of the text or after ``{``, ``[``, ``,``, ``:`` or a line
    end is a JSON or YAML string.
    """
    before = opening
    while before > 0 and text[before - 1] in " \t":
        before -= 1
    return 0 < before < opening and text[before - 1] not in "{[,:\n\r"


def _decode_shell_strings(text: str) -> tuple[str, list[int], list[int]]:
    """The double-quoted shell words of *text*, decoded the way a shell does it.

    A backslash is dropped only before ``"``, a backslash, ``$`` and a
    backtick. Every other backslash stays, so ``\\n`` and ``\\u0026`` are
    text here, not escapes. A string that is not closed on its line is
    skipped. Returns the same three lists as :func:`_decode_json_strings`.
    """
    parts: list[str] = []
    places: list[int] = []
    ends: list[int] = []
    pos = 0
    while (opening := text.find('"', pos)) != -1:
        pos = opening + 1
        if not _is_shell_word(text, opening):
            continue
        kept_parts, kept_places = len(parts), len(places)
        while (mark := _SHELL_MARK.search(text, pos)) is not None:
            at = mark.start()
            if at > pos:
                parts.append(text[pos:at])
                places.extend(range(pos, at))
            pos = at + 1
            if text[at] != "\\":
                break
            if text[at + 1 : at + 2] in _SHELL_ESCAPED:
                parts.append(text[at + 1])
                pos = at + 2
            else:
                parts.append("\\")
            places.append(at)
        if mark is not None and text[mark.start()] == '"':
            ends.append(len(places))
            parts.append("\n")
            places.append(mark.start())
            continue
        del parts[kept_parts:], places[kept_places:]
        if mark is None:
            break
    return "".join(parts), places, ends


def _decoded_spans(decoded: str, ends: list[int], raw: list[int]) -> list[tuple[int, int]]:
    """The secret values of the decoded strings, each cut at the end of its string."""
    spans: list[tuple[int, int]] = []
    for start, end in _plain_spans(decoded):
        end = min(end, ends[bisect.bisect_left(ends, start)])
        if start < end:
            spans.append((raw[start], raw[end]))
    return spans


def _shell_spans(text: str, origin: list[int]) -> list[tuple[int, int]]:
    """The secret values inside the double-quoted shell words of *text*, read as a shell does."""
    decoded, places, ends = _decode_shell_strings(text)
    return _decoded_spans(decoded, ends, [origin[place] for place in places]) if decoded else []


def _json_spans(text: str) -> list[tuple[int, int]]:
    """The secret values inside the JSON strings of *text*, up to three levels deep.

    Each decoded string is read like plain text. A value found there ends
    at the end of its string and is mapped back to *text*, so a mark
    replaces whole escapes and every JSON level still parses. On the text
    and on each decoded level, the double-quoted shell words are also read
    the way a shell reads them (:func:`_shell_spans`).
    """
    current, origin = text, list(range(len(text) + 1))
    spans = _shell_spans(current, origin)
    for _level in range(_JSON_LEVELS):
        decoded, places, ends = _decode_json_strings(current)
        if not decoded:
            break
        raw = [origin[place] for place in places]
        spans.extend(_decoded_spans(decoded, ends, raw))
        spans.extend(_shell_spans(decoded, raw))
        current, origin = decoded, raw
    return spans


def _plain_spans(text: str) -> list[tuple[int, int]]:
    return [*_long_option_spans(text), *_tool_spans(text), *_environment_spans(text)]


def _value_spans(text: str) -> list[tuple[int, int]]:
    """The secret values of every form, also in decoded JSON strings, sorted and merged."""
    return merge_spans([*_plain_spans(text), *_json_spans(text)])


@functools.lru_cache(maxsize=1)
def _cached_value_spans(text: str) -> tuple[tuple[int, int], ...]:
    """:func:`_value_spans`, kept for the last text.

    The two command line rules of a policy read the same text when the
    rules between them hid nothing, so the text is read only once.
    """
    return tuple(_value_spans(text))


#: Texts the first command line rule leaves with nothing for the second
#: one: every value it found is replaced by its mark. When the rules in
#: between hide nothing, the second rule gets one of these texts.
_SETTLED: Final[collections.deque[str]] = collections.deque(maxlen=4)


def _marked(text: str, spans: list[tuple[int, int]]) -> str:
    """*text* with each span replaced by the mark of the first command line rule."""
    parts: list[str] = []
    pos = 0
    for start, end in spans:
        parts += [text[pos:start], _OWN_MARK]
        pos = end
    return "".join([*parts, text[pos:]])


class CommandSecretFinder:
    """Report the secret value of each command line form listed in the module docstring.

    With *leave_to*, a value that pattern hides is left to it (see the
    module docstring). With *rest*, the finder is the second rule, after
    the *leave_to* pattern: a text that the first rule left with nothing
    to hide is not read again.
    """

    def __init__(self, leave_to: re.Pattern[str] | None = None, *, rest: bool = False) -> None:
        self._leave_to = leave_to
        self._rest = rest

    def secret_spans(self, text: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` of every secret value, left to right, never overlapping."""
        if self._rest and text in _SETTLED:
            return []
        spans = list(_cached_value_spans(text))
        if self._leave_to is None or not spans:
            return spans
        kept = _left_to(text, spans, self._leave_to)
        if kept == spans:
            _SETTLED.append(_marked(text, spans))
        return kept


#: The finder on its own: it reports every value it reads.
COMMAND_SECRET: Final[CommandSecretFinder] = CommandSecretFinder()
