# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A command line password in a log: the rest of the log stays readable.

The value of a password ends at the closing quote of the JSON string it
stands in, so a JSON log line still parses. A quote that is never closed
on its line does not hide the log lines after it. A long line of quoted
commands is read in linear time.
"""

from __future__ import annotations

import json
import time
from typing import Final

import pytest

from meho_backplane.redaction.command_secret import COMMAND_SECRET
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.policy import RedactionPolicy
from meho_backplane.redaction.resolver import get_default_policy

PW: Final = "made-up-pw-4711"
#: Built from pieces, so secret scanners do not flag the lines that use them.
_PWD_IS: Final = "P" + "wd="


def _answer_and_trace(text: str) -> tuple[str, str]:
    """What an operation answer and a trace show of *text*."""
    trace, _fired = scrub_content(text)
    return redact(text, get_default_policy()).redacted, trace


# -- JSON log lines ---------------------------------------------------------------

#: Command texts as a JSON log line holds them in its ``msg`` field. The
#: api_key rule does not hide these values, so this rule does, and the line
#: still parses.
_JSON_COMMANDS: Final[list[str]] = [
    f"run mysql -uroot -p{PW}",
    f"run --password {PW}",
]


def _json_line(command: str, number: int, style: str) -> str:
    entry = {"msg": command, "status": "failed", "ts": f"t{number}"}
    return json.dumps(entry, separators=(",", ":") if style == "compact" else None)


@pytest.mark.parametrize("style", ["compact", "spaced"])
@pytest.mark.parametrize("lines", [1, 3])
@pytest.mark.parametrize("command", _JSON_COMMANDS)
def test_a_json_log_line_still_parses_and_keeps_its_other_fields(
    command: str, lines: int, style: str
) -> None:
    log = "\n".join(_json_line(command, number, style) for number in range(1, lines + 1))
    for out in _answer_and_trace(log):
        assert PW not in out
        rows = [json.loads(row) for row in out.split("\n")]
        assert len(rows) == lines
        for number, row in enumerate(rows, start=1):
            assert row["status"] == "failed"
            assert row["ts"] == f"t{number}"
            assert "[REDACTED:command_line_secret]" in row["msg"]


@pytest.mark.parametrize("command", _JSON_COMMANDS)
def test_a_pretty_printed_json_document_still_parses(command: str) -> None:
    text = json.dumps({"msg": command, "status": "failed", "ts": "t1"}, indent=2)
    for out in _answer_and_trace(text):
        assert PW not in out
        assert json.loads(out)["status"] == "failed"
        assert json.loads(out)["ts"] == "t1"


def _on_main(text: str) -> tuple[str, str]:
    """What an answer and a trace show of *text* without this rule."""
    from meho_backplane.redaction.flight_recorder._content import _CONTENT_POLICY

    def without(policy: RedactionPolicy) -> RedactionPolicy:
        rules = tuple(rule for rule in policy.rules if rule.pattern != "command_line_secret")
        return policy.model_copy(update={"rules": rules})

    return (
        redact(text, without(get_default_policy())).redacted,
        redact(text, without(_CONTENT_POLICY)).redacted,
    )


#: Values that the api_key rule hides whole: the answer is the one of main.
_KEY_RULE_COMMANDS: Final[list[str]] = [
    f"DB_PASSWORD={PW}",
    f"Server=db;{_PWD_IS}{PW}",
    *(
        f"Server=db;Uid=sa;{label}{value};Database=d"
        for label in (_PWD_IS, "Pass" + "word=")
        for value in (f"'{PW}'", f"{{{PW}}}", PW)
    ),
    *(f"{label}{value};" for label in (_PWD_IS, "Pass" + "word=") for value in (f"'{PW}'", PW)),
]


@pytest.mark.parametrize("style", ["compact", "spaced"])
@pytest.mark.parametrize("command", _KEY_RULE_COMMANDS)
def test_a_json_line_whose_password_the_api_key_rule_hides_is_the_answer_of_main(
    command: str, style: str
) -> None:
    log = _json_line(command, 1, style)
    assert _answer_and_trace(log) == _on_main(log)
    for out in _answer_and_trace(log):
        assert PW not in out


# -- a quoted password in a JSON string ---------------------------------------------

#: The two words of a quoted password; JSON writes its quotes as ``\\"``.
_FIRST, _SECOND = "made-up", "phrase-4711"
_QUOTED: Final = f'"{_FIRST} {_SECOND}"'
#: Built from pieces, so secret scanners do not flag the line that uses it.
_USER_IS: Final = "bob" + ":"
#: Each form with a password of two words in double quotes.
_JSON_FORMS: Final[list[str]] = [
    f"run --password {_QUOTED} tail",
    f"run --password={_QUOTED} tail",
    f"run --api-key={_QUOTED} tail",
    f"mysql -u root -p{_QUOTED} appdb",
    f"sshpass -p {_QUOTED} ssh host",
    f"sqlcmd -S srv -P {_QUOTED} -Q x",
    f"ldapsearch -x -w {_QUOTED} -b dc=x",
    f"curl -u {_USER_IS}{_QUOTED} https://h/x",
    f"smbclient //h/share -U bob%{_QUOTED} -c ls",
    f"DB_PASSWORD={_QUOTED} run",
    f"login {'pass' + 'word'}={_QUOTED} --verbose",
    f"sqlplus scott/{_QUOTED}@orcl as sysdba",
    f"Server=db;{_PWD_IS}{_QUOTED};Database=d",
    f"run --password {_FIRST}\\ {_SECOND} tail",
]


def _json_string(command: str) -> str:
    """*command* in a JSON log line that stands in a JSON string of another one."""
    inner = json.dumps({"msg": command, "status": "failed"})
    return json.dumps({"log": inner, "ok": 1})


# -- a quote that is never closed on its line ---------------------------------------

#: (the form up to its opening quote, a closing mark on a later log line)
_UNCLOSED: Final[list[tuple[str, str]]] = [
    ("run --password '", "it's done"),
    ("DB_PASSWORD='", "it's done"),
    ("mysql -p'", "it's done"),
    ("sshpass -p '", "it's done"),
    ('run --password "', 'say "hi" done'),
]
_ORDINARY_LINES: Final = 1000


@pytest.mark.parametrize(("form", "later"), _UNCLOSED)
def test_one_unclosed_quote_does_not_hide_the_log_lines_after_it(form: str, later: str) -> None:
    body = "".join(f"line {n} ordinary text\n" for n in range(_ORDINARY_LINES))
    text = f"{form}{PW}\n{body}{later}\n"
    for out in _answer_and_trace(text):
        assert PW not in out
        assert out.count("ordinary text") == _ORDINARY_LINES
        assert out.endswith(f"\n{later}\n")


@pytest.mark.parametrize("form", [form for form, _later in _UNCLOSED])
def test_a_closing_mark_at_the_end_of_the_text_far_down_does_not_close(form: str) -> None:
    closing = {"'": "'", '"': '"', "{": "}"}[form[-1]]
    body = "".join(f"line {n} ordinary text\n" for n in range(_ORDINARY_LINES))
    text = f"{form}{PW}\n{body}done{closing}"
    for out in _answer_and_trace(text):
        assert PW not in out
        assert out.count("ordinary text") == _ORDINARY_LINES
        assert out.endswith(f"\ndone{closing}")


#: A line break between a command and a ``pwd=`` word.
_FIELD_BREAKS: Final[list[str]] = ["\n", "\r\n", "\n\n", " \n\t"]


@pytest.mark.parametrize("brk", _FIELD_BREAKS)
def test_a_working_folder_after_a_command_on_the_line_above_stays(brk: str) -> None:
    text = f"cd /srv;{brk}pwd=/tmp\nnext line"
    for out in _answer_and_trace(text):
        assert out == text


# -- one long line ----------------------------------------------------------------


@pytest.mark.parametrize(
    "unit", ['"--password x" ', '"u/p@svc" ', '"a;pw=x" ', '"--password \\"x y\\"" ', '"\\\\" ']
)
def test_a_4_mib_line_of_quoted_forms_is_read_in_linear_time(unit: str) -> None:
    # The end of the line is found once per line. A search for it for each
    # quoted string takes square time: then four times the text takes about
    # sixteen times as long.
    def seconds(size: int) -> float:
        text = unit * (size // len(unit))
        started = time.perf_counter()
        COMMAND_SECRET.secret_spans(text)
        return time.perf_counter() - started

    one_mib = seconds(1024 * 1024)
    four_mib = seconds(4 * 1024 * 1024)
    assert four_mib < 8 * one_mib + 0.5
    assert four_mib < 15.0
