#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when an install command holds a secret as an argument.

The install guide (``docs-site/install/``) and the install notes
(``deploy/charts/meho/templates/NOTES.txt``) show commands that people copy.
A secret (a password, a token, a key) that is an argument of a program is
in the process list and in the shell history. So a secret must come from a
file or from standard input::

    printf '%s' "$PW" | kubectl create secret generic <name> --from-file=<key>=/dev/stdin

``read`` and ``printf`` are built into bash and zsh. They start no program,
so the value is in no process list.

The check reads the fenced blocks of the pages (bash, sh, zsh, shell, console,
sql and no language) and every line of ``NOTES.txt``. It joins lines that
end in a backslash, then fails on:

* ``--from-literal=<key>=`` when the key name holds ``password``, ``passwd``,
  ``secret``, ``token``, ``key`` or ``credential``, or when the value is an
  address with a password (``://<user>:<something>@``);
* ``--password=``, ``--docker-password=``, ``--token=``, ``--client-secret=``;
* a ``<name>=<value>`` argument of ``vault`` or ``bao`` when the name is
  ``jwt``, ``token``, ``password`` or ``secret_id`` and the value is not ``-``
  or ``@<file>``;
* ``-H`` or ``--header`` with ``Authorization:`` in its value, also when
  the value is attached to ``-H`` (``-H"Authorization: ..."``), and
  ``Bearer $`` or ``Bearer <`` anywhere;
* ``PASSWORD '`` in a SQL block;
* a shell variable with a secret name that is expanded as an argument of a
  program: ``$NAME``, ``${NAME}``, and ``${NAME`` with an operator after the
  name (``${NAME:?}``, ``${NAME:-<word>}``, ``${NAME%<word>}``). The name holds ``PASSWORD``,
  ``PASSWD``, ``SECRET``, ``TOKEN`` or ``CREDENTIAL`` (any case), or has
  ``KEY`` or ``PW`` as a whole part between underscores (``API_KEY``,
  ``TEMP_PW``, ``KEY``; not ``PWD``, not ``KEYCLOAK_URL``). Only the
  built-ins ``printf``, ``[`` and ``test`` may get such a variable.

A ``--from-literal`` argument counts also when it is quoted as a whole
(``--from-literal="password=..."``).

Run ``python3 scripts/ci/check_install_secrets.py``. Exit code 1 lists each
line that breaks the rule.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES_DIR = "docs-site/install"
NOTES_FILE = "deploy/charts/meho/templates/NOTES.txt"

_COMMAND_LANGUAGES = {"", "bash", "sh", "zsh", "shell", "console", "sql"}
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")
_SECRET_KEY = re.compile(r"password|passwd|secret|token|key|credential", re.IGNORECASE)

_FROM_LITERAL = re.compile(r"""--from-literal=['"]?([^=\s'"]+)=('[^']*'|"[^"]*"|[^\s'"]*)""")
_ADDRESS_WITH_PASSWORD = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_SECRET_FLAG = re.compile(r"(?<![\w-])--(?:docker-password|password|token|client-secret)=")
_VAULT_COMMAND = re.compile(r"(?:^|[\s|;&(])(?:vault|bao)\s")
_VAULT_ARG = re.compile(r"""(?<![\w-])(jwt|token|password|secret_id)=('[^']*'|"[^"]*"|\S*)""")
_AUTH_HEADER = re.compile(
    r"""(?<![\w-])(?:-H|--header)(?:\s+|=)?"""
    r"""(?:"[^"]*Authorization:[^"]*"|'[^']*Authorization:[^']*'|\S*Authorization:\S*)""",
    re.IGNORECASE,
)
_BEARER = re.compile(r"Bearer\s+[$<]")
_SQL_PASSWORD = re.compile(r"\bPASSWORD\s+'", re.IGNORECASE)

_SECRET_NAME = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL", re.IGNORECASE)
_SECRET_NAME_PARTS = {"KEY", "PW"}
# ``${NAME}``, ``${NAME<operator>...}`` (group 2 is ``}`` or empty) or ``$NAME``.
_VARIABLE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(\}|(?=[-:=?+#%/^,@\[]))|([A-Za-z_][A-Za-z0-9_]*))"
)
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Words that start a shell construct; the program of the command comes after them.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time"}
# Built-ins that start no program, so their arguments are in no process list.
_BUILTINS_FOR_SECRETS = {"printf", "[", "test"}


def _literal_reasons(line: str) -> list[str]:
    reasons: list[str] = []
    for match in _FROM_LITERAL.finditer(line):
        key, value = match.group(1), match.group(2)
        if _SECRET_KEY.search(key):
            reasons.append(
                f"--from-literal holds a secret key '{key}': use --from-file=<key>=/dev/stdin"
            )
        elif _ADDRESS_WITH_PASSWORD.search(value):
            reasons.append(
                "--from-literal holds an address with a password: use --from-file=<key>=/dev/stdin"
            )
    return reasons


def _vault_reasons(line: str) -> list[str]:
    vault = _VAULT_COMMAND.search(line)
    if not vault:
        return []
    reasons: list[str] = []
    for match in _VAULT_ARG.finditer(line, vault.end()):
        value = match.group(2).strip("'\"")
        if value != "-" and not value.startswith("@"):
            reasons.append(
                f"vault argument '{match.group(1)}=' holds a value: use '{match.group(1)}=-'"
            )
    return reasons


def is_secret_name(name: str) -> bool:
    """Say whether a shell variable name names a secret."""
    if _SECRET_NAME.search(name):
        return True
    return any(part.upper() in _SECRET_NAME_PARTS for part in name.split("_"))


class _Word:
    """One word of a simple command: its text and the variables it expands."""

    def __init__(self) -> None:
        self.text = ""
        self.variables: list[str] = []


class _Splitter:
    """Split a command line into simple commands, quote-aware.

    A ``$(...)`` starts its own command. Text in single quotes expands
    nothing. A ``#`` that starts a word starts a comment. The ``}`` that
    closes a ``${NAME<operator>...}`` is part of the word.
    """

    _SEPARATORS = "|;&(){}`"

    def __init__(self, line: str) -> None:
        self.line = line
        self.pos = 0
        self.done: list[list[_Word]] = []
        self.stack: list[tuple[list[_Word], _Word, str]] = []
        self.command: list[_Word] = []
        self.word: _Word | None = None
        self.quote = ""
        self.braces = 0

    def _current(self) -> _Word:
        if self.word is None:
            self.word = _Word()
        return self.word

    def _end_word(self) -> None:
        if self.word is not None:
            self.command.append(self.word)
            self.word = None

    def _end_command(self) -> None:
        self._end_word()
        if self.command:
            self.done.append(self.command)
        self.command = []

    def _open_substitution(self) -> None:
        self.stack.append((self.command, self._current(), self.quote))
        self.command, self.word, self.quote = [], None, ""
        self.pos += 2

    def _separator(self, char: str) -> None:
        self._end_command()
        if char == ")" and self.stack:
            self.command, self.word, self.quote = self.stack.pop()
        self.pos += 1

    def _variable(self) -> bool:
        match = _VARIABLE.match(self.line, self.pos)
        if not match:
            return False
        word = self._current()
        word.variables.append(match.group(1) or match.group(3))
        word.text += match.group(0)
        if match.group(1) and not match.group(2):
            self.braces += 1
        self.pos = match.end()
        return True

    def _add(self, text: str) -> None:
        self._current().text += text
        self.pos += len(text)

    def _step(self) -> bool:
        """Read one item at ``pos``. Return False at a comment."""
        line, char = self.line, self.line[self.pos]
        if self.quote == "'":
            if char == "'":
                self.quote = ""
                self.pos += 1
            else:
                self._add(char)
        elif char == "\\":
            self._add(line[self.pos : self.pos + 2])
        elif char == "}" and self.braces:
            self.braces -= 1
            self._add(char)
        elif line.startswith("$(", self.pos) and not line.startswith("$((", self.pos):
            self._open_substitution()
        elif char == "$" and self._variable():
            pass
        elif char in "\"'" and self.quote in ("", char):
            self.quote = "" if self.quote else char
            self._current()
            self.pos += 1
        else:
            return self._plain(char)
        return True

    def _plain(self, char: str) -> bool:
        """Read one character outside a quote start, a ``$`` and a backslash."""
        if self.quote == '"':
            self._add(char)
        elif char == "#" and self.word is None:
            return False
        elif char in self._SEPARATORS:
            self._separator(char)
        elif char.isspace():
            self._end_word()
            self.pos += 1
        else:
            self._add(char)
        return True

    def split(self) -> list[list[_Word]]:
        while self.pos < len(self.line) and self._step():
            pass
        self._end_command()
        while self.stack:
            outer, _, _ = self.stack.pop()
            if outer:
                self.done.append(outer)
        return self.done


def _commands(line: str) -> list[list[_Word]]:
    """Split a command line into simple commands (see ``_Splitter``)."""
    return _Splitter(line).split()


def _program(command: list[_Word]) -> str | None:
    """Return the program name of a simple command, or None for a bare assignment."""
    for word in command:
        if word.text in _KEYWORDS or _ASSIGNMENT.match(word.text):
            continue
        return word.text
    return None


def _variable_reasons(line: str) -> list[str]:
    reasons: list[str] = []
    for command in _commands(line):
        program = _program(command)
        if program is None or program in _BUILTINS_FOR_SECRETS:
            continue
        for word in command:
            if word.text == program and not word.variables:
                continue
            reasons.extend(
                f"the secret variable '{name}' is an argument of '{program}': "
                "give it on standard input with printf"
                for name in word.variables
                if is_secret_name(name)
            )
    return reasons


def check_line(line: str, *, sql: bool = False) -> list[str]:
    """Return the reasons why one (joined) command line breaks the rule."""
    reasons = _literal_reasons(line) + _vault_reasons(line) + _variable_reasons(line)
    if _SECRET_FLAG.search(line):
        reasons.append("a secret is the value of a flag: read it from a file or standard input")
    if _AUTH_HEADER.search(line):
        reasons.append("an Authorization header is an argument: give it to curl with -H @-")
    if _BEARER.search(line):
        reasons.append("a bearer token is on the command line: give the header to curl with -H @-")
    if sql and _SQL_PASSWORD.search(line):
        reasons.append("a password is in SQL text: set it with \\password in psql")
    return reasons


def _join_continuations(lines: list[tuple[int, str]]) -> Iterator[tuple[int, str]]:
    """Join lines that end in a backslash. Yield (first line number, joined line)."""
    start = 0
    parts: list[str] = []
    for number, text in lines:
        if not parts:
            start = number
        stripped = text.rstrip()
        if stripped.endswith("\\"):
            parts.append(stripped[:-1])
            continue
        parts.append(text)
        yield start, " ".join(part.strip() for part in parts)
        parts = []
    if parts:
        yield start, " ".join(part.strip() for part in parts)


def command_blocks(text: str) -> Iterator[tuple[str, list[tuple[int, str]]]]:
    """Yield (language, [(line number, line), ...]) for each fenced block to check."""
    fence = ""
    language = ""
    block: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(line)
        if not fence:
            if match:
                fence, language, block = match.group(1), match.group(2).lower(), []
            continue
        if (
            match
            and match.group(1).startswith(fence[0])
            and len(match.group(1)) >= len(fence)
            and not match.group(2)
        ):
            if language in _COMMAND_LANGUAGES:
                yield language, block
            fence = ""
            continue
        block.append((number, line))


def check_page(text: str) -> list[tuple[int, str]]:
    """Check the fenced command blocks of one Markdown page."""
    found: list[tuple[int, str]] = []
    for language, lines in command_blocks(text):
        for number, joined in _join_continuations(lines):
            found.extend((number, reason) for reason in check_line(joined, sql=language == "sql"))
    return found


def check_notes(text: str) -> list[tuple[int, str]]:
    """Check every line of the install notes."""
    lines = list(enumerate(text.splitlines(), start=1))
    found: list[tuple[int, str]] = []
    for number, joined in _join_continuations(lines):
        found.extend((number, reason) for reason in check_line(joined))
    return found


def check_repo(root: Path) -> list[str]:
    """Check the install guide and the install notes under ``root``."""
    problems: list[str] = []
    for page in sorted((root / PAGES_DIR).rglob("*.md")):
        rel = page.relative_to(root)
        problems.extend(
            f"{rel}:{n}: {why}" for n, why in check_page(page.read_text(encoding="utf-8"))
        )
    notes = root / NOTES_FILE
    problems.extend(
        f"{NOTES_FILE}:{n}: {why}" for n, why in check_notes(notes.read_text(encoding="utf-8"))
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)"
    )
    args = parser.parse_args(argv)
    problems = check_repo(args.root)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} install command(s) hold a secret as an argument.", file=sys.stderr)
        return 1
    print("No install command holds a secret as an argument.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
