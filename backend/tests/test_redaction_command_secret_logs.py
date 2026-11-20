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
from meho_backplane.redaction.resolver import get_default_policy

PW: Final = "made-up-pw-4711"
#: Built from pieces, so secret scanners do not flag the lines that use them.
_PWD_IS: Final = "P" + "wd="


def _answer_and_trace(text: str) -> tuple[str, str]:
    """What an operation answer and a trace show of *text*."""
    trace, _fired = scrub_content(text)
    return redact(text, get_default_policy()).redacted, trace


# -- JSON log lines ---------------------------------------------------------------

#: Command texts as a JSON log line holds them in its ``msg`` field.
_JSON_COMMANDS: Final[list[str]] = [
    f"run mysql -uroot -p{PW}",
    f"run --password {PW}",
    f"DB_PASSWORD={PW}",
    f"Server=db;{_PWD_IS}{PW}",
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
            assert "[REDACTED:" in row["msg"]


@pytest.mark.parametrize("command", _JSON_COMMANDS)
def test_a_pretty_printed_json_document_still_parses(command: str) -> None:
    text = json.dumps({"msg": command, "status": "failed", "ts": "t1"}, indent=2)
    for out in _answer_and_trace(text):
        assert PW not in out
        assert json.loads(out)["status"] == "failed"
        assert json.loads(out)["ts"] == "t1"


# -- a quote that is never closed on its line ---------------------------------------

#: (the form up to its opening quote, a closing mark on a later log line)
_UNCLOSED: Final[list[tuple[str, str]]] = [
    ("run --password '", "it's done"),
    ("DB_PASSWORD='", "it's done"),
    ("mysql -p'", "it's done"),
    ("sshpass -p '", "it's done"),
    (f"Server=db;{_PWD_IS}'", "it's done"),
    (f"Server=db;{_PWD_IS}{{", "set a}b done"),
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


#: Shell forms whose quoted value runs over a line break and goes on after it.
_JOINED_FORMS: Final[list[str]] = [
    "run --password '",
    'run --password "',
    "run --password=x'",
    "mysql -p'",
    "sshpass -p '",
    "ldapsearch -w '",
    "DB_PASSWORD='",
]


@pytest.mark.parametrize("form", _JOINED_FORMS)
def test_a_quoted_part_over_a_line_break_keeps_the_parts_joined_to_it(form: str) -> None:
    quote = form[-1]
    text = f"{form}made-up\nsecond-part{quote}joined-tail more\nnext line"
    for out in _answer_and_trace(text):
        assert "made-up" not in out
        assert "second-part" not in out
        assert "joined-tail" not in out
        assert out.endswith(" more\nnext line")


@pytest.mark.parametrize("form", [form for form, _later in _UNCLOSED])
def test_a_real_quoted_value_over_a_line_break_is_still_hidden_whole(form: str) -> None:
    closing = {"'": "'", '"': '"', "{": "}"}[form[-1]]
    text = f"{form}made-up\nsecond-part{closing} tail\nnext line"
    for out in _answer_and_trace(text):
        assert "made-up" not in out
        assert "second-part" not in out
        assert out.endswith("\nnext line")


#: A line break between a connection string field and a password that starts with ``/``.
_FIELD_BREAKS: Final[list[str]] = ["\n", "\r\n", "\n\n", " \n\t"]


@pytest.mark.parametrize("brk", _FIELD_BREAKS)
def test_a_connection_password_after_a_line_break_is_not_a_working_folder(brk: str) -> None:
    text = f"Server=db;{brk}{_PWD_IS}/made up phrase\nnext line"
    for out in _answer_and_trace(text):
        assert "made up phrase" not in out
        assert out.startswith(f"Server=db;{brk}{_PWD_IS}")
        assert out.endswith("\nnext line")


@pytest.mark.parametrize("brk", _FIELD_BREAKS)
def test_a_working_folder_after_a_command_on_the_line_above_stays(brk: str) -> None:
    text = f"cd /srv;{brk}pwd=/tmp\nnext line"
    for out in _answer_and_trace(text):
        assert out == text


# -- one long line ----------------------------------------------------------------


@pytest.mark.parametrize("unit", ['"--password x" ', '"u/p@svc" ', '"a;pw=x" '])
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
