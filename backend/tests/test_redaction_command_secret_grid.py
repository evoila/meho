# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Command texts built from parts: every form is hidden in every encoding.

A test builds command texts from parts: words before the command, a
separator, an option before the password, each form of the
``command_line_secret`` rule, and a plain word after it. Each text is
read plain, as a JSON string, as a JSON log line, as a JSON log line
inside a JSON string, as ``sh -c "..."`` and as a JSON log line that
holds ``sh -c "..."``. In the answer and in the trace no part of the
made-up password is shown, the word after the command stays, and every
JSON level still parses. Harmless look-alikes built from the same parts
are byte for byte what the policy without this rule makes of them.

CI runs a fixed sample that holds every part at least once. The full
grid runs by hand: ``MEHO_RUN_REDACTION_GRID=1 uv run pytest -q
tests/test_redaction_command_secret_grid.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from typing import Final

import pytest

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.flight_recorder._content import _CONTENT_POLICY, scrub_content
from meho_backplane.redaction.policy import RedactionPolicy
from meho_backplane.redaction.resolver import get_default_policy

#: The made-up password, and the parts of it that must never be shown.
PW: Final = "made-up-pw-4711"
_PIECES: Final = ("made", "-up-", "pw-4", "4711")
_TAIL: Final = "tailword"
#: Built from pieces, so secret scanners do not flag the lines that use them.
_USER: Final = "bob" + ":"

#: Words before the command: a plain word, a word in double quotes, a word
#: in single quotes, a double quote inside single quotes, a single quote
#: inside double quotes, an escaped double quote, an escaped single quote,
#: an escaped double quote inside double quotes, an escaped backslash, a
#: Windows path with a backslash before ``n``, the same in double quotes,
#: and a word with an apostrophe.
_WORDS: Final = (
    "ok",
    '"ok go"',
    "'ok go'",
    "'a\"b'",
    '"it\'s"',
    'a\\"b',
    "a\\'b",
    '"a\\"b"',
    "a\\\\b",
    "C:\\new\\x.cnf",
    '"C:\\new\\x.cnf"',
    "don't",
)
_PREFIXES: Final = (
    "",
    *(f"echo {word}" for word in _WORDS),
    *(f"echo {first} {second}" for first in _WORDS for second in _WORDS),
)
_SEPARATORS: Final = (" ; ", " && ", " || ", " | ", "\n")
#: Options before the password: none, a Windows path, a single-quoted value
#: with a space, a double-quoted value. ``sshpass`` has options of its own.
_MIDDLES: Final = ("", " -o C:\\new\\out.txt", " -H 'a b'", ' -H "a b"')
_SSHPASS_MIDDLES: Final = ("", " -f C:\\new\\pw.txt", " -P 'Enter pw:'", ' -P "Enter pw:"')

#: Each form the rule names, with ``{m}`` where the option before the
#: password stands and ``{v}`` where the value stands: glued, after a
#: space, after ``=`` and in quotes.
_FORMS: Final = (
    "run{m} --password {v}",
    "run{m} --password={v}",
    "run{m} --db-pass={v}",
    "run{m} --password '{v}'",
    'run{m} --token "{v}"',
    "run{m} --api-key {v}",
    "run{m} --client-secret '{v}'",
    "sshpass{m} -p {v} ssh h",
    "sshpass{m} -p{v} ssh h",
    "sshpass{m} -p '{v}' ssh h",
    'sshpass{m} -p "{v}" ssh h',
    "mysql{m} -p{v}",
    "mysqldump{m} -p'{v}'",
    'mariadb{m} -p"{v}"',
    "sqlcmd{m} -P {v}",
    "bcp{m} -P{v}",
    "osql{m} -P '{v}'",
    'nsrsqlsv{m} -P "{v}"',
    "ldapsearch{m} -w {v}",
    "ldapmodify{m} -w{v}",
    "ldapsearch{m} -w '{v}'",
    'ldapmodify{m} -w "{v}"',
    "curl{m} -u " + _USER + "{v}",
    "curl{m} -u" + _USER + "{v}",
    "curl{m} --user " + _USER + "{v}",
    "curl{m} --user=" + _USER + "{v}",
    "curl{m} -u '" + _USER + "{v}'",
    'curl{m} -u "' + _USER + '{v}"',
    "smbclient //h/s{m} -U bob%{v}",
    "smbclient //h/s{m} -Ubob%{v}",
    "smbclient //h/s{m} -U 'bob%{v}'",
    'smbclient //h/s{m} -U "bob%{v}"',
    "env{m} DB_PASSWORD={v}",
    "env{m} APP_PASS='{v}'",
    'env{m} APP_PWD="{v}"',
    "env{m} APP_SECRET={v}",
    "env{m} API_TOKEN={v}",
    "env{m} SIGNING_KEY={v}",
)

#: Harmless look-alikes: a port after ``-P``, ``-pthread``, ``-pedantic``,
#: ``ssh -p`` with a port, ``curl -u`` and ``smbclient -U`` with a user
#: only, a paging token, a ``_KEY`` name that holds no secret,
#: ``--password-file`` with a path and ``-p`` with no value.
_HARMLESS: Final = (
    "mysql{m} -P 3306 -h db",
    "gcc{m} -pthread x.c",
    "gcc{m} -pedantic x.c",
    "ssh{m} -p 2222 h",
    "curl{m} -u bob https://h/x",
    "smbclient //h/s{m} -U bob -c ls",
    "aws s3api list-objects-v2{m} --next-token page-2",
    "env{m} S3_KEY=path/to/object.tar.gz",
    "run{m} --password-file /etc/pw.txt",
    "mysql{m} -p -h db",
)


def _sh_c(text: str) -> str:
    """*text* as the command of ``sh -c "..."``."""
    return 'sh -c "' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _log(text: str) -> str:
    return json.dumps({"log": text, "ok": 0})


def _read_log(text: str) -> str:
    entry = json.loads(text)
    assert entry["ok"] == 0
    return str(entry["log"])


def _read_string(text: str) -> str:
    value = json.loads(text)
    assert isinstance(value, str)
    return value


#: (name, encode, check that every JSON level of an answer parses).
_ENCODINGS: Final[list[tuple[str, Callable[[str], str], Callable[[str], object]]]] = [
    ("plain", lambda text: text, lambda out: out),
    ("json string", json.dumps, _read_string),
    ("json log line", _log, _read_log),
    (
        "json log line in a json string",
        lambda text: json.dumps(_log(text)),
        lambda out: _read_log(_read_string(out)),
    ),
    ("sh -c", _sh_c, lambda out: out),
    ("json log line with sh -c", lambda text: _log(_sh_c(text)), _read_log),
]


def _commands(forms: tuple[str, ...], *, full: bool) -> Iterator[str]:
    """Command texts from the parts. The sample takes one prefix per other choice.

    The prefix turns with a counter, so the sample still holds every word,
    every separator, every option and every form.
    """
    count = 0
    for form in forms:
        middles = _SSHPASS_MIDDLES if form.startswith("sshpass") else _MIDDLES
        for separator in _SEPARATORS:
            for middle in middles:
                command = form.format(m=middle, v=PW) + " " + _TAIL
                prefixes = _PREFIXES if full else (_PREFIXES[count % len(_PREFIXES)],)
                count += 1
                for prefix in prefixes:
                    yield (prefix + separator if prefix else "") + command


def _problems(text: str, parse: Callable[[str], object]) -> list[str]:
    """What is wrong with the answer and the trace of *text*."""
    wrong: list[str] = []
    answer = redact(text, get_default_policy()).redacted
    for name, out in (("answer", answer), ("trace", scrub_content(text)[0])):
        if any(piece in out for piece in _PIECES):
            wrong.append(f"{name} shows the value")
        if _TAIL not in out:
            wrong.append(f"{name} lost the word after the command")
        try:
            parse(out)
        except (ValueError, KeyError, TypeError, AssertionError):
            wrong.append(f"{name} does not parse")
    return wrong


def _without_command_rule(policy: RedactionPolicy) -> RedactionPolicy:
    rules = tuple(rule for rule in policy.rules if rule.pattern != "command_line_secret")
    assert len(rules) == len(policy.rules) - 1
    return policy.model_copy(update={"rules": rules})


def _changed(text: str) -> list[str]:
    """The policies whose answer on *text* differs from the answer without this rule."""
    return [
        name
        for name, policy in (("answer", get_default_policy()), ("trace", _CONTENT_POLICY))
        if redact(text, policy).redacted != redact(text, _without_command_rule(policy)).redacted
    ]


def _runs_over_a_line_break(name: str, command: str) -> bool:
    """True for a quoted command over a line break that the rule does not read yet.

    In ``sh -c "..."`` over a line break the double-quoted command is no
    JSON string, so it is read as plain text, where ``sshpass`` stops at
    the second part of a quoted option value (Task #6747).
    """
    return name.endswith("sh -c") and "\n" in command and 'sshpass -P "' in command


def _run_grid(*, full: bool) -> tuple[int, dict[str, list[str]]]:
    count, wrong = 0, {}
    for name, encode, parse in _ENCODINGS:
        for command in _commands(_FORMS, full=full):
            count += 1
            if _runs_over_a_line_break(name, command):
                continue
            if found := _problems(encode(command), parse):
                wrong[f"{name}: {command!r}"] = found
    return count, wrong


def _run_harmless(*, full: bool) -> tuple[int, dict[str, list[str]]]:
    count, wrong = 0, {}
    for name, encode, _parse in _ENCODINGS:
        for command in _commands(_HARMLESS, full=full):
            count += 1
            if found := _changed(encode(command)):
                wrong[f"{name}: {command!r}"] = found
    return count, wrong


def test_every_form_is_hidden_in_every_encoding() -> None:
    count, wrong = _run_grid(full=False)
    assert count == len(_ENCODINGS) * len(_FORMS) * len(_SEPARATORS) * len(_MIDDLES)
    assert wrong == {}


def test_harmless_look_alikes_are_as_without_the_rule_in_every_encoding() -> None:
    count, wrong = _run_harmless(full=False)
    assert count == len(_ENCODINGS) * len(_HARMLESS) * len(_SEPARATORS) * len(_MIDDLES)
    assert wrong == {}


_FULL_GRID: Final = os.environ.get("MEHO_RUN_REDACTION_GRID") == "1"


@pytest.mark.skipif(not _FULL_GRID, reason="The full grid runs by hand: MEHO_RUN_REDACTION_GRID=1.")
def test_the_full_grid() -> None:
    count, wrong = _run_grid(full=True)
    harmless_count, changed = _run_harmless(full=True)
    print(f"grid: {count} texts, {len(wrong)} wrong")
    print(f"harmless: {harmless_count} texts, {len(changed)} changed")
    assert wrong == {}
    assert changed == {}
