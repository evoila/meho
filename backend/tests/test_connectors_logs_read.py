# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared log read: ``linux.logs.read`` and ``k8s.logs.read``."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_logs_read import k8s_logs_read
from meho_backplane.connectors.kubernetes.ops_logs_window import (
    collect_window,
    window_from_params,
)
from meho_backplane.connectors.linux import LINUX_OPS, LinuxSshConnector
from meho_backplane.connectors.linux.logs_journal import build_journal_command, journal_query
from meho_backplane.connectors.linux.ops_file import (
    CONTENT_MARKER,
    MISSING_MARKER,
    UNREADABLE_MARKER,
)
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError
from meho_backplane.connectors.log_read import (
    LOG_READ_RESPONSE_SCHEMA,
    MAX_LINE_CHARS,
    MAX_LOG_LINES,
    LogEntry,
    LogReadError,
    build_log_answer,
    parse_since,
    parse_until,
    redact_entries,
    take_page,
)

_PASSWORD = "made-up-pw-4711"
_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
_END = "-----END RSA " + "PRIVATE KEY-----"
_KEY_BODY = [f"madeUpKeyBody{i:02d}" for i in range(6)]
_KEY = [_BEGIN, *_KEY_BODY, _END]
_LINUX_TARGET = MagicMock()
_OPERATOR = Operator(
    sub="op-logs-read",
    name="Logs Read",
    email=None,
    raw_jwt="op.logs.jwt",
    tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
    tenant_role=TenantRole.OPERATOR,
)


def _proc(stdout: str, exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.exit_status = exit_status
    return proc


def _file_output(lines: list[str], path: str = "/var/log/app.log", size: int = 100) -> str:
    hex_path = (path + "\n").encode().hex()
    text = "".join(f"{line}\n" for line in lines)
    return f"{hex_path}\n{size}\n{CONTENT_MARKER}\n{text}"


_T0 = 1_790_000_000_000_000
_BOOT = "b" * 32


def _cursor(number: int) -> str:
    return f"s={'a' * 32};i={number:x};b={_BOOT};m=1;t=2;x=3"


def _journal_output(entries: list[tuple[int, str]], *, cut: bool = False) -> str:
    """What the host prints; the number of an entry is its offset from ``_T0``."""
    rows = [
        json.dumps(
            {
                "__CURSOR": _cursor(number),
                "__REALTIME_TIMESTAMP": str(_T0 + number),
                "MESSAGE": message,
            }
        )
        for number, message in entries
    ]
    return "\n".join(rows) + ("\nMEHO_JOURNAL_CUT\n" if cut else "\nMEHO_JOURNAL_RC=0\n")


class _Journal:
    """A host whose journal holds *messages*; it answers the built command like journalctl."""

    def __init__(self, messages: list[str]) -> None:
        self.entries = list(enumerate(messages))
        self.commands: list[str] = []

    async def run(self, _target: Any, cmd: str, **_: Any) -> Any:
        self.commands.append(cmd)
        entries = self.entries
        marker = re.search(r"--cursor='?(s=[^ ']+)'?", cmd)
        if "--reverse" in cmd:
            entries = entries[::-1]
        if marker is not None:
            numbers = [_cursor(number) for number, _ in entries]
            if marker.group(1) in numbers:
                entries = entries[numbers.index(marker.group(1)) :]
        rows = int(re.search(r"-v rows=(\d+)", cmd).group(1))  # type: ignore[union-attr]
        return _proc(_journal_output(entries[:rows], cut=len(entries) > rows))

    async def read(self, **params: Any) -> dict[str, Any]:
        connector = LinuxSshConnector()
        with patch.object(connector, "_run_command", side_effect=self.run):
            answer = await connector.logs_read(_LINUX_TARGET, {"source": "journal", **params})
        Draft202012Validator(LOG_READ_RESPONSE_SCHEMA).validate(answer)
        return answer


def _lines_of(answer: dict[str, Any]) -> list[str]:
    return [row["line"] for row in answer["rows"]]


async def _linux(params: dict[str, Any], stdout: str) -> tuple[dict[str, Any], str]:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as run:
        run.return_value = _proc(stdout)
        result = await connector.logs_read(_LINUX_TARGET, params)
    return result, run.await_args_list[0].args[1]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_ops_are_safe_reads_in_group_logs() -> None:
    linux = next(op for op in LINUX_OPS if op.op_id == "linux.logs.read")
    k8s = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.logs.read")
    for op in (linux, k8s):
        assert op.group_key == "logs"
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert set(op.parameter_schema["properties"]) == {
            "source",
            "since",
            "until",
            "lines",
            "filter",
        }
        Draft202012Validator.check_schema(op.parameter_schema)


def test_old_ops_keep_their_names_and_parameters() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    assert "path" in by_id["linux.log.tail"].parameter_schema["properties"]
    assert by_id["linux.journal.tail"].group_key == "log"
    k8s = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.logs")
    assert {"pod_name", "namespace", "tail", "since_time"} <= set(
        k8s.parameter_schema["properties"]
    )


def test_schema_rejects_bad_since_and_extra_keys() -> None:
    op = next(op for op in LINUX_OPS if op.op_id == "linux.logs.read")
    validator = Draft202012Validator(op.parameter_schema)
    assert validator.is_valid({"source": "journal", "since": "15m"})
    assert validator.is_valid({"source": "journal", "since": "2026-11-10T08:00:00Z"})
    assert not validator.is_valid({"source": "journal", "since": "yesterday"})
    assert not validator.is_valid({"source": "journal", "lines": 5001})
    assert not validator.is_valid({"source": "journal", "path": "/x"})
    assert not validator.is_valid({"since": "15m"})
    assert validator.is_valid({"source": "journal", "since": _cursor(1)})
    assert validator.is_valid({"source": "journal", "until": _cursor(1)})
    k8s = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.logs.read")
    assert not Draft202012Validator(k8s.parameter_schema).is_valid(
        {"source": "n/p", "since": _cursor(1)}
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_parse_since_span_and_time() -> None:
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
    assert parse_since("15m", now) == datetime(2026, 11, 10, 11, 45, 0, tzinfo=UTC)
    assert parse_since("2026-11-10T08:00:00+02:00") == datetime(2026, 11, 10, 6, 0, 0, tzinfo=UTC)
    assert parse_since(None) is None
    assert parse_until(None) is None


def test_parse_time_without_offset_is_refused() -> None:
    with pytest.raises(LogReadError):
        parse_until("2026-11-10T08:00:00")


def test_parse_time_keeps_microseconds_and_rounds_into_the_window() -> None:
    assert parse_since("2026-11-10T08:00:00.1234561Z") == datetime(
        2026, 11, 10, 8, 0, 0, 123457, tzinfo=UTC
    )
    assert parse_since("2026-11-10T08:00:00.123456000Z") == datetime(
        2026, 11, 10, 8, 0, 0, 123456, tzinfo=UTC
    )
    assert parse_until("2026-11-10T08:00:00.1234569Z") == datetime(
        2026, 11, 10, 8, 0, 0, 123456, tzinfo=UTC
    )


def test_a_span_that_is_too_long_is_refused() -> None:
    with pytest.raises(LogReadError):
        parse_since("999999999999d")


def _entries(*lines: str) -> list[LogEntry]:
    return [LogEntry(time=f"2026-01-01T00:00:0{i}Z", line=line) for i, line in enumerate(lines)]


async def test_page_keeps_the_asked_end_and_says_truncated() -> None:
    entries = _entries("0", "1", "2", "3", "4")
    newest = build_log_answer(await take_page(entries, lines=2, needle=None, keep="newest"))
    assert _lines_of(newest) == ["3", "4"]
    assert newest["truncated"] is True
    assert newest["covered"] == {"from": "2026-01-01T00:00:03Z", "to": "2026-01-01T00:00:04Z"}
    oldest = await take_page(entries, lines=2, needle=None, keep="oldest")
    answer = build_log_answer(oldest, continuation="marker")
    assert _lines_of(answer) == ["0", "1"]
    assert answer["continuation"] == "marker"


async def test_answer_without_times_or_cut() -> None:
    page = await take_page([LogEntry(time=None, line="a")], lines=5, needle=None, keep="newest")
    answer = build_log_answer(page)
    assert answer["covered"] is None
    assert answer["truncated"] is False
    assert answer["total"] == 1
    empty = build_log_answer(await take_page([], lines=5, needle=None, keep="oldest"))
    assert empty["rows"] == [] and empty["total"] == 0 and empty["covered"] is None


async def test_covered_is_the_searched_span_not_the_span_of_the_matches() -> None:
    entries = _entries("a", "b", "needle", "c", "d")
    whole = {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:04Z"}
    for keep in ("oldest", "newest"):
        none = build_log_answer(await take_page(entries, lines=5, needle="absent", keep=keep))
        assert none["rows"] == [] and none["covered"] == whole
        one = build_log_answer(await take_page(entries, lines=5, needle="needle", keep=keep))
        assert _lines_of(one) == ["needle"] and one["covered"] == whole


async def test_covered_ends_where_a_cut_page_ends() -> None:
    entries = _entries("x1", "-", "x2", "-", "x3")
    page = await take_page(entries, lines=1, needle="x", keep="oldest")
    assert build_log_answer(page)["covered"] == {
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-01-01T00:00:01Z",
    }


async def test_long_line_is_matched_whole_and_marked_when_cut() -> None:
    long = "a" * 9000 + "needle" + "b" * 9000
    page = await take_page([LogEntry(None, long)], lines=5, needle="needle", keep="newest")
    row = page.rows[0]
    assert row["line_truncated"] is True
    assert "needle" in row["line"] and len(row["line"]) == MAX_LINE_CHARS
    plain = await take_page([LogEntry(None, "short")], lines=5, needle=None, keep="newest")
    assert plain.rows[0] == {"time": None, "line": "short", "line_truncated": False}


async def test_a_cut_line_does_not_part_a_secret_from_its_name() -> None:
    secret = "madeUpSecret" * 400
    line = json.dumps({"dbPassword": secret, "note": "needle"})
    page = await take_page([LogEntry(None, line)], lines=5, needle="needle", keep="newest")
    assert "needle" in page.rows[0]["line"]
    assert "madeUpSecret" not in page.rows[0]["line"]


async def test_the_filter_does_not_match_a_hidden_value() -> None:
    entries = [LogEntry(None, json.dumps({"dbPassword": _PASSWORD}))]
    page = await take_page(entries, lines=5, needle=_PASSWORD, keep="newest")
    assert page.rows == []


async def _redacted(lines: list[str]) -> list[str]:
    entries = await redact_entries(
        [LogEntry(None, line) for line in lines],
        connector=LinuxSshConnector(),
        operator=_OPERATOR,
        op_id="linux.logs.read",
    )
    return [entry.line for entry in entries]


@pytest.mark.parametrize(
    "lines",
    [
        ["before", *_KEY, "after"],
        ["before", _BEGIN, *_KEY_BODY],
        [*_KEY_BODY, _END, "after"],
        ["before", *_KEY, "between", *_KEY, "after"],
        ["before", *_KEY, *_KEY_BODY, _END, "after"],
    ],
    ids=["whole", "end missing", "start missing", "two keys", "second start missing"],
)
async def test_a_key_over_many_lines_is_hidden(lines: list[str]) -> None:
    shown = await _redacted(lines)
    assert len(shown) == len(lines)
    assert not any(part in "\n".join(shown) for part in _KEY_BODY)
    for word in ("before", "between", "after"):
        assert (word in lines) == (word in shown)


async def test_a_lone_end_line_hides_only_the_lines_a_key_can_have() -> None:
    lines = [f"line {i}" for i in range(400)]
    shown = await _redacted([*lines, _END])
    assert shown[:100] == lines[:100]
    assert "line 399" not in shown


# ---------------------------------------------------------------------------
# linux.logs.read
# ---------------------------------------------------------------------------


async def test_linux_file_read_returns_rows_without_times() -> None:
    result, cmd = await _linux(
        {"source": "/var/log/app.log", "lines": 10}, _file_output(["a", "b"])
    )
    assert "tail -n 266" in cmd
    assert result["rows"] == [
        {"time": None, "line": "a", "line_truncated": False},
        {"time": None, "line": "b", "line_truncated": False},
    ]
    assert result["covered"] is None and result["continuation"] is None
    assert result["truncated"] is False
    Draft202012Validator(LOG_READ_RESPONSE_SCHEMA).validate(result)


async def test_linux_file_read_marks_truncated_when_older_lines_are_left() -> None:
    result, _ = await _linux(
        {"source": "/var/log/app.log", "lines": 2}, _file_output(["a", "b", "c"])
    )
    assert _lines_of(result) == ["b", "c"] and result["truncated"] is True
    exact, _ = await _linux({"source": "/var/log/app.log", "lines": 2}, _file_output(["a", "b"]))
    assert exact["truncated"] is False


async def test_linux_file_that_is_empty_is_an_empty_answer() -> None:
    result, _ = await _linux({"source": "/var/log/app.log"}, _file_output([], size=0))
    assert result["rows"] == [] and result["truncated"] is False


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (MISSING_MARKER + "\n", "does not exist"),
        (UNREADABLE_MARKER + "\n", "could not be opened"),
        (_file_output([], size=4096), "gave no content"),
    ],
    ids=["missing", "unreadable", "opened but not read"],
)
async def test_linux_file_that_was_not_read_is_an_error(stdout: str, message: str) -> None:
    with pytest.raises(LinuxInspectError, match=message):
        await _linux({"source": "/var/log/app.log"}, stdout)


async def test_linux_file_filter_reads_wide_window_and_keeps_matches() -> None:
    out = _file_output(["ok", "ERROR one", "ok", "ERROR two"])
    result, cmd = await _linux({"source": "/var/log/app.log", "lines": 1, "filter": "ERROR"}, out)
    assert "tail -n 10000" in cmd
    assert [r["line"] for r in result["rows"]] == ["ERROR two"]
    assert result["truncated"] is True


async def test_linux_file_refuses_since_and_until() -> None:
    for extra in ({"since": "15m"}, {"until": "2026-11-10T08:00:00Z"}):
        with pytest.raises(LinuxInspectError):
            await _linux({"source": "/var/log/app.log", **extra}, "")


async def test_linux_file_outside_allow_list_is_refused_before_any_command() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as run:
        with pytest.raises(Exception, match=r"allow|root|under"):
            await connector.logs_read(_LINUX_TARGET, {"source": "/etc/shadow-copy/../../root/x"})
        run.assert_not_awaited()


async def test_linux_journal_unit_with_window_has_times_and_covered() -> None:
    out = _journal_output([(0, "start"), (60_000_000, "stop")])
    result, cmd = await _linux(
        {"source": "nfs-server.service", "since": "2026-11-10T08:00:00Z", "lines": 50}, out
    )
    assert "-u nfs-server.service" in cmd
    assert "--since='2026-11-10 08:00:00.000000 UTC'" in cmd
    assert "--reverse" not in cmd
    assert [r["line"] for r in result["rows"]] == ["start", "stop"]
    assert result["covered"] == {
        "from": "2026-09-21T14:13:20+00:00",
        "to": "2026-09-21T14:14:20+00:00",
    }
    assert result["continuation"] == _cursor(60_000_000)


async def test_linux_journal_keeps_the_fraction_of_both_times() -> None:
    _, cmd = await _linux(
        {
            "source": "journal",
            "since": "2026-11-10T08:00:00.9Z",
            "until": "2026-11-10T10:00:01.123456+02:00",
        },
        _journal_output([(0, "x")]),
    )
    assert "--since='2026-11-10 08:00:00.900000 UTC'" in cmd
    assert "--until='2026-11-10 08:00:01.123456 UTC'" in cmd


async def test_linux_journal_forward_pages_read_every_entry_once() -> None:
    journal = _Journal([f"m{i}" for i in range(7)])
    page = await journal.read(since="1h", lines=3)
    seen = _lines_of(page)
    assert seen == ["m0", "m1", "m2"] and page["truncated"] is True
    assert page["continuation"] == _cursor(2)
    while page["truncated"]:
        page = await journal.read(since=page["continuation"], lines=3)
        seen += _lines_of(page)
    assert seen == [f"m{i}" for i in range(7)]
    assert f"--cursor='{_cursor(2)}'" in journal.commands[1]
    assert "--since" not in journal.commands[1]
    # The last marker stays usable: later entries follow it.
    journal.entries.append((7, "m7"))
    assert _lines_of(await journal.read(since=page["continuation"])) == ["m7"]


async def test_linux_journal_reads_back_from_the_newest_page() -> None:
    journal = _Journal([f"m{i}" for i in range(5)])
    newest = await journal.read(lines=2)
    assert _lines_of(newest) == ["m3", "m4"] and newest["truncated"] is True
    assert newest["continuation"] == _cursor(3)
    older = await journal.read(until=newest["continuation"], lines=10)
    assert _lines_of(older) == ["m0", "m1", "m2"] and older["truncated"] is False
    assert all("--reverse" in cmd for cmd in journal.commands)


async def test_linux_journal_marker_stays_when_nothing_follows() -> None:
    journal = _Journal(["m0"])
    answer = await journal.read(since=_cursor(0))
    assert answer["rows"] == [] and answer["truncated"] is False
    assert answer["continuation"] == _cursor(0) and answer["covered"] is None


async def test_linux_journal_marker_that_is_gone_is_an_error() -> None:
    journal = _Journal(["m0", "m1"])
    with pytest.raises(LinuxInspectError, match="marker"):
        await journal.read(since=_cursor(99))


async def test_linux_journal_until_marker_needs_a_read_without_since() -> None:
    with pytest.raises(LogReadError, match="without 'since'"):
        journal_query({"source": "journal", "since": "15m", "until": _cursor(1)})


async def test_linux_journal_filter_without_a_match_says_what_was_searched() -> None:
    journal = _Journal([f"m{i}" for i in range(4)])
    answer = await journal.read(since="1h", filter="absent")
    assert answer["rows"] == [] and answer["truncated"] is False
    assert answer["covered"]["from"].endswith("14:13:20+00:00")
    assert answer["covered"]["to"].endswith("14:13:20.000003+00:00")
    one = await journal.read(since="1h", filter="m2")
    assert _lines_of(one) == ["m2"] and one["covered"] == answer["covered"]


async def test_linux_journal_filter_matches_behind_the_cut_of_a_long_message() -> None:
    journal = _Journal(["short", "x" * 6000 + " needle"])
    answer = await journal.read(since="1h", filter="needle")
    assert answer["total"] == 1
    assert answer["rows"][0]["line_truncated"] is True
    assert "needle" in answer["rows"][0]["line"]


async def test_linux_journal_entry_over_the_byte_cap_is_an_error() -> None:
    with pytest.raises(LinuxInspectError, match="larger than 8 MiB"):
        await _linux({"source": "journal"}, "MEHO_JOURNAL_CUT\n")


async def test_linux_journal_key_over_entries_is_hidden_on_every_page() -> None:
    journal = _Journal(["before", *_KEY, "after"])
    shown: list[str] = []
    page = await journal.read(since="1h", lines=4)
    shown += _lines_of(page)
    while page["truncated"]:
        page = await journal.read(since=page["continuation"], lines=4)
        shown += _lines_of(page)
    assert len(shown) == len(_KEY) + 2 and shown[0] == "before" and shown[-1] == "after"
    assert not any(part in "\n".join(shown) for part in _KEY_BODY)
    matched = await journal.read(since="1h", filter="madeUpKeyBody")
    assert matched["rows"] == []


async def test_linux_journal_keeps_the_warning_of_journalctl() -> None:
    connector = LinuxSshConnector()
    proc = _proc(_journal_output([(0, "x")]))
    proc.stderr = "Hint: some messages are not shown"
    with patch.object(connector, "_run_command", new_callable=AsyncMock, return_value=proc):
        answer = await connector.logs_read(_LINUX_TARGET, {"source": "journal"})
    assert answer["warning"] == "Hint: some messages are not shown"


@pytest.mark.parametrize(
    ("rows", "max_bytes", "cut"),
    [(5, 10_000, False), (3, 10_000, False), (2, 10_000, True), (5, 30, True)],
    ids=["all", "exact", "row cap", "byte cap"],
)
def test_linux_journal_command_caps_rows_and_bytes(
    tmp_path: Path, rows: int, max_bytes: int, cut: bool
) -> None:
    """The real shell command, with a made-up ``journalctl`` that prints three entries."""
    fake = tmp_path / "journalctl"
    fake.write_text('#!/bin/sh\nfor i in 1 2 3; do echo \'{"MESSAGE":"m"}\'; done\n')
    fake.chmod(0o755)
    cmd = build_journal_command(journal_query({"source": "journal"}), rows, max_bytes)
    out = subprocess.run(
        ["sh", "-c", cmd],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    ).stdout
    lines = [line for line in out.split("\n") if line]
    assert lines[-1] == ("MEHO_JOURNAL_CUT" if cut else "MEHO_JOURNAL_RC=0")
    assert len(lines) - 1 <= rows


def test_linux_journal_command_reports_a_failed_journalctl(tmp_path: Path) -> None:
    fake = tmp_path / "journalctl"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    cmd = build_journal_command(journal_query({"source": "journal"}), 5)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True, env=env)
    assert out.stdout.strip() == "MEHO_JOURNAL_RC=3"


async def test_linux_whole_journal_has_no_unit_flag() -> None:
    _, cmd = await _linux({"source": "journal"}, _journal_output([(0, "x")]))
    assert " -u " not in cmd


async def test_linux_journal_span_becomes_exact_start_time() -> None:
    _, cmd = await _linux({"source": "journal", "since": "15m"}, _journal_output([(0, "x")]))
    assert "--since='" in cmd and " UTC'" in cmd


async def test_linux_journal_failure_is_an_error_not_an_empty_answer() -> None:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as run:
        run.return_value = _proc("\nMEHO_JOURNAL_RC=1\n")
        with pytest.raises(LinuxInspectError):
            await connector.logs_read(_LINUX_TARGET, {"source": "journal"})


async def test_linux_journal_bad_unit_name_is_refused() -> None:
    with pytest.raises(ValueError):
        await _linux({"source": "bad unit; rm -rf /"}, "")


async def test_password_in_a_log_line_comes_back_hidden() -> None:
    line = f"login ok password={_PASSWORD} user=svc"
    result, _ = await _linux({"source": "/var/log/app.log"}, _file_output(["fine", line]))
    assert _PASSWORD not in json.dumps(result)
    assert result["rows"][0]["line"] == "fine"


async def test_linux_file_key_over_lines_is_hidden_also_with_a_filter_and_a_line_cut() -> None:
    out = _file_output(["before", *_KEY, "after"])
    for extra in ({}, {"lines": 4}, {"filter": "madeUpKeyBody"}, {"filter": "e"}):
        result, _ = await _linux({"source": "/var/log/app.log", **extra}, out)
        assert not any(part in json.dumps(result) for part in _KEY_BODY)


# ---------------------------------------------------------------------------
# k8s.logs.read
# ---------------------------------------------------------------------------


async def _k8s(
    params: dict[str, Any], page: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    inner = AsyncMock(return_value={"pod": "p", "namespace": "n", "container": "c", **page})
    with patch("meho_backplane.connectors.kubernetes.ops_logs_read.k8s_logs", inner):
        result = await k8s_logs_read(KubernetesConnector(), MagicMock(), _OPERATOR, params)
    return result, inner.await_args.args[3]


async def test_k8s_tail_read_maps_source_and_filter() -> None:
    result, inner = await _k8s(
        {"source": "prod/web-1/app", "lines": 2, "filter": "boom"},
        {"lines": ["ok", "boom 1", "boom 2", "boom 3"], "truncated": False},
    )
    assert inner == {"pod_name": "web-1", "namespace": "prod", "container": "app", "tail": 5000}
    assert [r["line"] for r in result["rows"]] == ["boom 2", "boom 3"]
    assert result["truncated"] is True


async def test_k8s_source_without_container() -> None:
    _, inner = await _k8s({"source": "prod/web-1"}, {"lines": [], "truncated": False})
    assert "container" not in inner and inner["tail"] == 456
    _, most = await _k8s(
        {"source": "prod/web-1", "lines": MAX_LOG_LINES}, {"lines": [], "truncated": False}
    )
    assert most["tail"] == 5000


async def test_k8s_since_reads_a_window_with_times_and_keeps_the_marker() -> None:
    page = {
        "lines": ["2026-11-10T08:00:00.1Z first", "2026-11-10T08:00:01.2Z second"],
        "truncated": True,
        "next_since_time": "2026-11-10T08:00:02.3Z",
    }
    result, inner = await _k8s({"source": "n/p", "since": "2026-11-10T08:00:00Z"}, page)
    assert inner["since_time"] == "2026-11-10T08:00:00Z" and "tail" not in inner
    assert result["rows"][0] == {
        "time": "2026-11-10T08:00:00.1Z",
        "line": "first",
        "line_truncated": False,
    }
    assert result["covered"] == {"from": "2026-11-10T08:00:00.1Z", "to": "2026-11-10T08:00:01.2Z"}
    assert result["continuation"] == "2026-11-10T08:00:02.3Z"
    assert result["truncated"] is True


async def test_k8s_lines_cut_in_a_window_continues_at_first_dropped_row() -> None:
    page = {
        "lines": [f"2026-11-10T08:00:0{i}Z l{i}" for i in range(4)],
        "truncated": False,
    }
    result, _ = await _k8s({"source": "n/p", "since": "2026-11-10T08:00:00Z", "lines": 2}, page)
    assert [r["line"] for r in result["rows"]] == ["l0", "l1"]
    assert result["continuation"] == "2026-11-10T08:00:02Z"
    assert result["truncated"] is True


async def test_k8s_exact_times_go_to_the_reader_as_they_are() -> None:
    since, until = "2026-11-10T08:00:00.123456789Z", "2026-11-10T09:00:00.9+01:00"
    _, inner = await _k8s(
        {"source": "n/p", "since": since, "until": until}, {"lines": [], "truncated": False}
    )
    assert inner["since_time"] == since and inner["until_time"] == until
    _, span = await _k8s({"source": "n/p", "since": "15m"}, {"lines": [], "truncated": False})
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", span["since_time"])


class _PodLog:
    """A container log, read with the real window code of ``k8s.logs``."""

    def __init__(self, lines: list[str]) -> None:
        self.body = "".join(f"{line}\n" for line in lines).encode()

    async def _chunks(self) -> AsyncIterator[bytes]:
        yield self.body

    async def _logs(self, *args: Any) -> dict[str, Any]:
        window = window_from_params(args[3])
        assert window is not None
        page = await collect_window(self._chunks(), window, cap_bytes=1024 * 1024)
        answer: dict[str, Any] = {"lines": page.lines, "truncated": page.truncated}
        if page.truncated:
            answer["next_since_time"] = page.next_time
        return answer

    async def read(self, **params: Any) -> dict[str, Any]:
        path = "meho_backplane.connectors.kubernetes.ops_logs_read.k8s_logs"
        with patch(path, self._logs):
            answer = await k8s_logs_read(
                KubernetesConnector(), MagicMock(), _OPERATOR, {"source": "n/p", **params}
            )
        Draft202012Validator(LOG_READ_RESPONSE_SCHEMA).validate(answer)
        return answer

    async def read_all(self, **params: Any) -> list[str]:
        seen: list[str] = []
        for _ in range(50):
            page = await self.read(**params)
            seen += _lines_of(page)
            if not page["truncated"]:
                return seen
            assert page["continuation"] != params["since"], "the read does not move on"
            params["since"] = page["continuation"]
        raise AssertionError("the read does not end")


async def test_k8s_pages_inside_one_second_read_every_line_once() -> None:
    log = _PodLog([f"2026-11-10T08:00:00.{i}00000001Z l{i}" for i in range(10)])
    first = await log.read(since="2026-11-10T08:00:00Z", lines=3)
    assert first["continuation"] == "2026-11-10T08:00:00.300000001Z"
    assert await log.read_all(since="2026-11-10T08:00:00Z", lines=3) == [f"l{i}" for i in range(10)]
    some = await log.read(
        since="2026-11-10T08:00:00.200000001Z", until="2026-11-10T08:00:00.400000001Z"
    )
    assert _lines_of(some) == ["l2", "l3", "l4"]


async def test_k8s_page_ends_between_two_times() -> None:
    stamps = ["01", "02", "02", "02", "03"]
    log = _PodLog([f"2026-11-10T08:00:{s}Z l{i}" for i, s in enumerate(stamps)])
    first = await log.read(since="2026-11-10T08:00:00Z", lines=3)
    assert _lines_of(first) == ["l0"] and first["truncated"] is True
    assert first["continuation"] == "2026-11-10T08:00:02Z"
    second = await log.read(since=first["continuation"], lines=3)
    assert _lines_of(second) == ["l1", "l2", "l3"]
    third = await log.read(since=second["continuation"], lines=3)
    assert _lines_of(third) == ["l4"] and third["truncated"] is False


async def test_k8s_more_lines_at_one_time_than_lines_is_an_error() -> None:
    log = _PodLog(["2026-11-10T08:00:01Z a", "2026-11-10T08:00:01Z b"])
    with pytest.raises(LogReadError, match="share the time 2026-11-10T08:00:01Z"):
        await log.read(since="2026-11-10T08:00:00Z", lines=1)
    assert _lines_of(await log.read(since="2026-11-10T08:00:00Z", lines=1, filter="b")) == ["b"]


async def test_k8s_filter_pages_and_says_what_was_searched() -> None:
    log = _PodLog([f"2026-11-10T08:00:0{i}Z {'hit' if i in (2, 7) else 'x'}{i}" for i in range(9)])
    none = await log.read(since="2026-11-10T08:00:00Z", filter="absent")
    whole = {"from": "2026-11-10T08:00:00Z", "to": "2026-11-10T08:00:08Z"}
    assert none["rows"] == [] and none["covered"] == whole
    first = await log.read(since="2026-11-10T08:00:00Z", filter="hit", lines=1)
    assert first["covered"] == {"from": whole["from"], "to": "2026-11-10T08:00:06Z"}
    assert first["continuation"] == "2026-11-10T08:00:07Z"
    assert await log.read_all(since="2026-11-10T08:00:00Z", filter="hit", lines=1) == [
        "hit2",
        "hit7",
    ]


async def test_k8s_key_over_lines_is_hidden_on_every_page_and_in_the_newest_lines() -> None:
    stamped = [f"2026-11-10T08:00:{i:02d}Z {line}" for i, line in enumerate(["before", *_KEY, "z"])]
    log = _PodLog(stamped)
    shown = await log.read_all(since="2026-11-10T08:00:00Z", lines=1)
    assert len(shown) == len(stamped) and shown[0] == "before" and shown[-1] == "z"
    assert not any(part in "\n".join(shown) for part in _KEY_BODY)
    assert (await log.read(since="2026-11-10T08:00:00Z", filter="madeUpKeyBody"))["rows"] == []
    newest, _ = await _k8s(
        {"source": "n/p", "lines": 4}, {"lines": ["before", *_KEY, "z"], "truncated": False}
    )
    assert not any(part in json.dumps(newest) for part in _KEY_BODY)


async def test_k8s_until_is_passed_on_and_needs_since() -> None:
    _, inner = await _k8s(
        {"source": "n/p", "since": "2026-11-10T08:00:00Z", "until": "2026-11-10T09:00:00Z"},
        {"lines": [], "truncated": False},
    )
    assert inner["until_time"] == "2026-11-10T09:00:00Z"
    with pytest.raises(LogReadError):
        await _k8s({"source": "n/p", "until": "2026-11-10T09:00:00Z"}, {"lines": []})


@pytest.mark.parametrize("source", ["nopod", "a//b", "a/b/c/d", "/b"])
async def test_k8s_bad_source_is_refused(source: str) -> None:
    with pytest.raises(LogReadError):
        await _k8s({"source": source}, {"lines": []})


async def test_k8s_password_in_a_log_line_comes_back_hidden() -> None:
    result, _ = await _k8s(
        {"source": "n/p"},
        {"lines": ["start", f"connect password={_PASSWORD}"], "truncated": False},
    )
    assert _PASSWORD not in json.dumps(result)
