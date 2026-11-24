# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared log read at its limits: what is before a page, large lines, exact counts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meho_backplane.connectors.kubernetes import KubernetesConnector, logs_read_source
from meho_backplane.connectors.kubernetes.ops_logs_read import k8s_logs_read
from meho_backplane.connectors.linux import LinuxSshConnector, logs_file, ops_file
from meho_backplane.connectors.linux.logs_file import (
    FileLines,
    build_log_file_command,
    parse_log_file_content,
)
from meho_backplane.connectors.linux.logs_journal import (
    build_context_command,
    build_journal_command,
    journal_query,
    parse_journal_entries,
)
from meho_backplane.connectors.linux.ops_file import CONTENT_MARKER
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError
from meho_backplane.connectors.log_read import (
    MAX_CONTEXT_LINE_CHARS,
    REDACTION_CONTEXT_LINES,
    LogContext,
    LogEntry,
    bound_context_line,
    redact_entries,
)
from tests._log_read_helpers import (
    START,
    FakeJournal,
    FakePodLog,
    cursor,
    journal_line,
    journal_time,
    proc,
    run_shell,
)

_MIB = 1024 * 1024
_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
_END = "-----END RSA " + "PRIVATE KEY-----"
_BODY = [f"madeUpKeyBody{i:03d}" for i in range(120)]
_KEY = [_BEGIN, *_BODY, _END]


def _lines_of(answer: dict[str, Any]) -> list[str]:
    return [row["line"] for row in answer["rows"]]


def _assert_hidden(answer: dict[str, Any], total: int) -> None:
    assert answer["total"] == total
    assert "madeUpKeyBody" not in json.dumps(answer)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


async def _redacted(lines: list[str], context: LogContext) -> list[str]:
    entries = await redact_entries(
        [LogEntry(None, line) for line in lines],
        context=context,
        connector=LinuxSshConnector(),
        operator=None,
        op_id="linux.logs.read",
    )
    return [entry.line for entry in entries]


async def test_a_key_that_starts_in_the_context_is_hidden() -> None:
    shown = await _redacted([*_BODY[:5], _END, "after"], LogContext(["x", _BEGIN]))
    assert len(shown) == 7 and shown[-1] == "after"
    assert "madeUpKeyBody" not in "".join(shown)
    closed = await _redacted(["plain"], LogContext([_BEGIN, "body", _END]))
    assert closed == ["plain"]


async def test_lines_that_can_be_a_key_body_are_hidden_when_the_context_is_not_known() -> None:
    lines = [_BODY[0], f"app: {'A' * 64}", "a plain line", "another: failed", ""]
    shown = await _redacted(lines, LogContext(["x y"], complete=False))
    assert shown == ["[REDACTED:pem_private_key]"] * 2 + lines[2:]
    # The context says where the key is, or the line is too far away for one.
    for context in (LogContext([_END], complete=False), LogContext(["x"])):
        assert (await _redacted(lines, context))[0] == _BODY[0]
    far = ["-"] * REDACTION_CONTEXT_LINES
    assert (await _redacted([*far, _BODY[0]], LogContext(complete=False)))[-1] == _BODY[0]
    marked = await _redacted([_BEGIN, _END, _BODY[0]], LogContext(complete=False))
    assert marked[-1] == _BODY[0]


def test_a_long_context_line_keeps_its_last_key_mark() -> None:
    line = "a" * 9000 + _END + "b" * 9000 + _BEGIN + "c" * 9000
    short = bound_context_line(line)
    assert len(short) < MAX_CONTEXT_LINE_CHARS + 200 and short.endswith(_BEGIN)
    assert len(bound_context_line("a" * 9000)) == MAX_CONTEXT_LINE_CHARS
    assert bound_context_line(_BEGIN) == _BEGIN


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


async def _journal(journal: FakeJournal, **params: Any) -> dict[str, Any]:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", side_effect=journal.run):
        return await connector.logs_read(MagicMock(), {"source": "journal", **params})


async def test_journal_window_inside_a_key_shows_no_part_of_it() -> None:
    journal = FakeJournal(["before", *_KEY, "after"])
    window = {"since": journal_time(40), "until": journal_time(59)}
    _assert_hidden(await _journal(journal, **window), 20)
    _assert_hidden(await _journal(journal, **window, filter="madeUpKeyBody"), 0)
    # Backwards: the newest entries up to 'until', with the start of the key before them.
    _assert_hidden(await _journal(journal, until=journal_time(59), lines=5), 5)
    _assert_hidden(await _journal(journal, until=cursor(60), lines=5), 5)
    _assert_hidden(await _journal(journal, since=cursor(60), lines=5), 5)


async def test_journal_large_entries_before_a_page_do_not_hide_the_start_of_a_key() -> None:
    journal = FakeJournal([f"{'x ' * _MIB}{_BEGIN}", *["y " * _MIB] * 8, *_BODY, _END, "after"])
    answer = await _journal(journal, since=journal_time(20), lines=10)
    _assert_hidden(answer, 10)


async def test_journal_entries_that_fit_one_by_one_are_read_one_by_one() -> None:
    big = ["1 " + "x " * (5 * _MIB // 2), "2 " + "y " * (5 * _MIB // 2), "3 " + "z " * 10]
    journal = FakeJournal(big)
    first = await _journal(journal, since=START, lines=10)
    assert [line[0] for line in _lines_of(first)] == ["1"] and first["truncated"] is True
    second = await _journal(journal, since=first["continuation"], lines=10)
    assert [line[0] for line in _lines_of(second)] == ["2", "3"] and second["truncated"] is False
    newest = await _journal(journal, lines=10)
    assert [line[0] for line in _lines_of(newest)] == ["2", "3"] and newest["truncated"] is True
    older = await _journal(journal, until=newest["continuation"], lines=10)
    assert [line[0] for line in _lines_of(older)] == ["1"] and older["truncated"] is False


async def test_journal_backward_marker_that_is_gone_is_an_error() -> None:
    with pytest.raises(LinuxInspectError, match="marker"):
        await _journal(FakeJournal(["m0"]), until=cursor(9))


async def test_journal_rotated_during_the_read_is_an_error() -> None:
    journal = FakeJournal(["m0", "m1", "m2"])

    async def run(target: Any, cmd: str, **kwargs: Any) -> Any:
        answer = await journal.run(target, cmd, **kwargs)
        journal.entries = journal.entries[-1:]
        return answer

    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_run_command", side_effect=run),
        pytest.raises(LinuxInspectError, match="rotated during the read"),
    ):
        await connector.logs_read(MagicMock(), {"source": "journal", "since": START})


async def test_journal_entry_without_a_marker_leaves_the_context_unknown() -> None:
    entry = json.dumps({"MESSAGE": _BODY[0], "__REALTIME_TIMESTAMP": "1"})
    connector = LinuxSshConnector()
    out = proc(f"{entry}\nMEHO_JOURNAL_RC=0\n")
    with patch.object(connector, "_run_command", return_value=out):
        answer = await connector.logs_read(MagicMock(), {"source": "journal", "since": START})
    _assert_hidden(answer, 1)


async def test_journal_without_entries_is_an_empty_answer() -> None:
    connector = LinuxSshConnector()
    out = proc("-- No entries --\n\nMEHO_JOURNAL_RC=0\n")
    with patch.object(connector, "_run_command", return_value=out):
        answer = await connector.logs_read(MagicMock(), {"source": "journal", "since": START})
    assert answer["rows"] == [] and answer["truncated"] is False


@pytest.mark.parametrize("extra", [0, 1])
def test_journal_exact_byte_limit_is_not_a_cut(extra: int) -> None:
    text = "".join(journal_line(i, "m") + "\n" for i in range(3))
    cmd = build_journal_command(journal_query({"source": "journal"}), 10, len(text) - extra)
    entries, cut = parse_journal_entries(run_shell("journalctl() { cat; }; " + cmd, text))
    assert len(entries) == 3 - extra and cut is bool(extra)


_AWKS = ["gawk", "mawk", "busybox"]


def _with_awk(cmd: str, awk: str) -> str:
    if shutil.which(awk) is None:
        pytest.skip(f"{awk} is not installed")
    program = "busybox awk" if awk == "busybox" else awk
    return cmd.replace("LC_ALL=C awk", f"LC_ALL=C {program}")


@pytest.mark.parametrize("awk", _AWKS)
def test_journal_context_command_sends_large_entries_short(awk: str) -> None:
    text = "".join(
        journal_line(number, message) + "\n"
        for number, message in enumerate(
            [
                "small",
                "x" * 20000 + '"\\ ' + _BEGIN + "y" * 20000,
                "z" * 20000,
                [0x2D] * 9000,
            ]
        )
    )
    cmd = "journalctl() { cat; }; " + build_context_command(None, cursor(0), 10)
    out = run_shell(_with_awk(cmd, awk), text)
    entries, cut = parse_journal_entries(out)
    assert [entry.marker for entry in entries] == [cursor(i) for i in range(4)] and not cut
    assert entries[0].line == "small" and entries[2].line == ""
    assert entries[1].line.endswith(_BEGIN) and len(entries[1].line) < 200
    assert entries[3].line == "-" * 9000


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------


def _read_file(path: Path, rows: int, awk: str | None = None, **caps: int) -> tuple[FileLines, str]:
    cmd = build_log_file_command(str(path), rows, **caps)
    out = run_shell(cmd if awk is None else _with_awk(cmd, awk))
    return parse_log_file_content(out.partition(CONTENT_MARKER + "\n")[2], str(path)), out


async def _linux_file(path: Path, **params: Any) -> dict[str, Any]:
    async def run(_target: Any, cmd: str, **_: Any) -> Any:
        out = run_shell(cmd)
        assert len(out.encode()) < 16 * _MIB, "over the output limit of SSH"
        return proc(out)

    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_run_command", side_effect=run),
        patch.object(logs_file, "confine_read_path", lambda path: path),
        patch.object(ops_file, "confine_read_path", lambda path: path),
    ):
        return await connector.logs_read(MagicMock(), {"source": str(path), **params})


async def test_file_short_last_line_after_long_lines_is_read(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text(("x" * 65536 + "\n") * 256 + "last\n")
    answer = await _linux_file(log, lines=1)
    assert _lines_of(answer) == ["last"] and answer["truncated"] is True
    # A key mark at the end of a long line is still found.
    log.write_text("x" * 65536 + f" {_BEGIN}\n" + ("y" * 65536 + "\n") * 254 + f"{_BODY[0]}\n")
    _assert_hidden(await _linux_file(log, lines=1), 1)


@pytest.mark.parametrize("params", [{"lines": 5}, {"lines": 5, "filter": "madeUpKeyBody"}])
async def test_file_that_ends_inside_a_key_shows_no_part_of_it(
    tmp_path: Path, params: dict[str, Any]
) -> None:
    log = tmp_path / "app.log"
    log.write_text("\n".join(["before", *_KEY[:-1]]) + "\n")
    _assert_hidden(await _linux_file(log, **params), 0 if "filter" in params else 5)


@pytest.mark.parametrize("older", [0, 1])
async def test_file_exact_line_limit_is_not_truncated(tmp_path: Path, older: int) -> None:
    log = tmp_path / "app.log"
    log.write_text("".join(f"{i}\n" for i in range(5000 + older)))
    answer = await _linux_file(log, lines=5000)
    assert answer["total"] == 5000 and answer["truncated"] is bool(older)
    log.write_text("x\n" * (10000 + older))
    filtered = await _linux_file(log, filter="absent")
    assert filtered["rows"] == [] and filtered["truncated"] is bool(older)


async def test_file_last_line_over_the_byte_cap_is_an_error(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("a\n" + "x" * 9 * _MIB + "\n")
    with pytest.raises(LinuxInspectError, match="larger than 8 MiB"):
        await _linux_file(log, lines=5)


async def test_file_that_is_empty_is_an_empty_answer(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("")
    answer = await _linux_file(log)
    assert answer["rows"] == [] and answer["truncated"] is False


@pytest.mark.parametrize("awk", _AWKS)
def test_file_command_splits_page_and_context(tmp_path: Path, awk: str) -> None:
    log = tmp_path / "app.log"
    long = b"\xff\xc3" * 3000 + _BEGIN.encode() + b"\xff" * 3000
    log.write_bytes(b"first\n" + long + b"\ncr\r\nno newline")
    read, out = _read_file(log, 2, awk)
    assert read.lines == ["cr", "no newline"] and read.cut and read.context.complete
    assert read.context.lines[0] == "first"
    assert read.context.lines[1].endswith(_BEGIN) and len(read.context.lines[1]) < 4400
    assert out.isascii()
    whole, _ = _read_file(tmp_path / "app.log", 1, awk, max_bytes=100)
    assert whole.lines == ["no newline"] and len(whole.context.lines) == 3
    log.write_text("".join(f"line {i}\n" for i in range(1000)))
    most, _ = _read_file(log, 10, awk)
    assert most.lines == [f"line {i}" for i in range(990, 1000)]
    assert most.context.lines == [f"line {i}" for i in range(990 - 256, 990)]


@pytest.mark.parametrize("awk", _AWKS)
def test_file_byte_caps_drop_the_oldest_lines(tmp_path: Path, awk: str) -> None:
    log = tmp_path / "app.log"
    log.write_text("".join(f"line {i}\n" for i in range(10)))
    capped, _ = _read_file(log, 10, awk, max_bytes=14)
    assert capped.lines == ["line 8", "line 9"] and capped.cut
    assert len(capped.context.lines) == 8 and capped.context.complete
    # The host looked at 20 bytes: the first of them are inside a line.
    scanned, _ = _read_file(log, 10, awk, scan_bytes=20)
    assert scanned.lines == ["line 8", "line 9"] and scanned.cut
    assert not scanned.context.complete


# ---------------------------------------------------------------------------
# Kubernetes
# ---------------------------------------------------------------------------


def _stamp(second: int) -> str:
    hours, rest = divmod(second, 3600)
    return f"2026-11-10T{hours:02d}:{rest // 60:02d}:{rest % 60:02d}Z"


async def _k8s(log: FakePodLog, **params: Any) -> dict[str, Any]:
    with log.patched():
        return await k8s_logs_read(
            KubernetesConnector(),
            MagicMock(),
            MagicMock(tenant_id=None),
            {"source": "n/p", **params},
        )


async def test_k8s_window_inside_a_key_shows_no_part_of_it() -> None:
    # A line each minute, so most of the key is more than five minutes before the window.
    log = FakePodLog([f"{_stamp(i * 60)} {line}" for i, line in enumerate(["-", *_KEY, "after"])])
    window = {"since": _stamp(6000), "until": _stamp(6540)}
    _assert_hidden(await _k8s(log, **window), 10)
    _assert_hidden(await _k8s(log, **window, filter="madeUpKeyBody"), 0)
    _assert_hidden(await _k8s(log, since=_stamp(6000), lines=5), 5)
    after = await _k8s(log, since=_stamp(6000))
    assert _lines_of(after)[-1] == "after"


async def test_k8s_log_that_ends_inside_a_key_shows_no_part_of_it() -> None:
    log = FakePodLog([f"{_stamp(i)} {line}" for i, line in enumerate(["-", *_KEY[:-1]])])
    _assert_hidden(await _k8s(log, lines=5), 5)
    _assert_hidden(await _k8s(log, lines=5, filter="madeUpKeyBody"), 0)


async def test_k8s_looks_back_only_as_far_as_needed() -> None:
    busy = FakePodLog([f"{_stamp(i)} l{i}" for i in range(1000)])
    answer = await _k8s(busy, since=_stamp(900))
    assert answer["total"] == 100
    assert [query.get("sinceTime") for query in busy.queries] == [_stamp(600), _stamp(900)]
    quiet = FakePodLog([f"{_stamp(i * 600)} l{i}" for i in range(10)])
    await _k8s(quiet, since=_stamp(3000))
    assert [query.get("sinceTime") for query in quiet.queries] == [
        _stamp(2700),
        "2026-11-09T00:50:00Z",
        None,
        _stamp(3000),
    ]
    marked = FakePodLog([f"{_stamp(i)} {_END if i == 899 else 'x'}" for i in range(1000)])
    await _k8s(marked, since=_stamp(900))
    assert [query.get("sinceTime") for query in marked.queries] == [_stamp(600), _stamp(900)]


@pytest.mark.parametrize("cap", [10, 1500])
async def test_k8s_too_much_log_before_a_window_hides_lines_that_can_be_a_key(
    monkeypatch: pytest.MonkeyPatch, cap: int
) -> None:
    """The look back is too large at once, or only from the start of the log."""
    monkeypatch.setattr(logs_read_source, "_MAX_LOOK_BACK_BYTES", cap)
    lines = [*["plain text"] * 60, *_BODY, "after the key"]
    log = FakePodLog([f"{_stamp(i * 100)} {line}" for i, line in enumerate(lines)])
    answer = await _k8s(log, since=_stamp(100 * 100))
    assert _lines_of(answer)[-1] == "after the key"
    _assert_hidden(answer, 81)


@pytest.mark.parametrize("older", [0, 1])
async def test_k8s_exact_line_limit_is_not_truncated(older: int) -> None:
    log = FakePodLog([f"{_stamp(i)} l{i}" for i in range(5000 + older)])
    answer = await _k8s(log, lines=5000)
    assert answer["total"] == 5000 and answer["truncated"] is bool(older)
    assert _lines_of(answer)[-1] == f"l{4999 + older}"
    filtered = await _k8s(log, filter="absent")
    assert filtered["rows"] == [] and filtered["truncated"] is bool(older)


async def test_k8s_newest_lines_stop_at_the_byte_cap() -> None:
    log = FakePodLog([f"{_stamp(i)} {'x ' * (_MIB // 4)}{i}" for i in range(5)])
    answer = await _k8s(log, lines=5)
    assert answer["total"] == 1 and answer["truncated"] is True
    one = FakePodLog([f"{_stamp(0)} {'x ' * _MIB}"])
    alone = await _k8s(one, lines=5)
    assert alone["total"] == 1 and alone["truncated"] is False
