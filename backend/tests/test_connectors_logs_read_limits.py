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
from meho_backplane.connectors.log_key_text import HIDDEN_KEY_TEXT, hide_key_text
from meho_backplane.connectors.log_read import (
    MAX_CONTEXT_LINE_CHARS,
    LogEntry,
    LogReadError,
    bound_context_line,
    build_log_answer,
    redact_entries,
    take_page,
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


#: Text of other services that stands between the lines of a key.
_OTHER = [f"svc: request {i} ok" for i in range(300)]


async def _redacted(lines: list[str], context: list[str]) -> list[str]:
    entries = await redact_entries(
        [LogEntry(None, line) for line in lines],
        context=context,
        connector=LinuxSshConnector(),
        operator=None,
        op_id="linux.logs.read",
    )
    return [entry.line for entry in entries]


async def test_a_key_that_starts_in_the_context_is_hidden() -> None:
    shown = await _redacted([*_BODY[:5], _END, "after"], ["x", _BEGIN])
    assert len(shown) == 7 and shown[-1] == "after"
    assert "madeUpKeyBody" not in "".join(shown)
    closed = await _redacted(["plain"], [_BEGIN, "body", _END])
    assert closed == ["plain"]


def test_text_that_can_be_a_key_body_is_hidden_whatever_is_before_it() -> None:
    mixed = "MIIEvQIBADANBgkqhkiG9w0BAQEF"
    body = [mixed, "A" * 64, f"app: {'A' * 64}", f"app[1]: {mixed}==", "c2Vj", "aB3dE6gH8j+="]
    shown = hide_key_text([*body[:4], "next", *body[4:]])
    assert shown[:4] == [
        HIDDEN_KEY_TEXT,
        HIDDEN_KEY_TEXT,
        f"app: {HIDDEN_KEY_TEXT}",
        f"app[1]: {HIDDEN_KEY_TEXT}",
    ]
    assert shown[4] == "next" and shown[6] == HIDDEN_KEY_TEXT
    # The short last line of a body.
    assert hide_key_text([mixed, "c2Vj", "after it", "c2Vj"])[1:] == [
        HIDDEN_KEY_TEXT,
        "after it",
        "c2Vj",
    ]


def test_ordinary_log_text_is_not_hidden() -> None:
    lines = [
        "",
        "done",
        "Started",
        "1234567890",
        "========================",
        "/var/log/messages",
        "open /var/lib/app/data/current failed",
        "java.lang.IllegalArgumentException: bad value",
        "digest sha256:" + "0123456789abcdef" * 4,
        "trace=4bf92f3577b34da6a3ce929d0e0e4736 status=200",
    ]
    assert hide_key_text(lines) == lines


async def test_hidden_key_text_is_said_in_the_answer() -> None:
    entries = await redact_entries(
        [LogEntry("t1", _BODY[0]), LogEntry("t2", "plain text")],
        context=[],
        connector=LinuxSshConnector(),
        operator=None,
        op_id="linux.logs.read",
    )
    # The filter does not see the hidden text, and the answer says so.
    page = await take_page(entries, lines=5, needle="madeUp", keep="newest")
    answer = build_log_answer(page, warning="from the source")
    assert answer["rows"] == []
    assert answer["warning"].startswith("from the source; 1 of the lines")
    plain = await take_page(entries[1:], lines=5, needle=None, keep="newest")
    assert "warning" not in build_log_answer(plain)


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


async def test_journal_key_that_starts_far_before_the_page_is_hidden() -> None:
    """The first line of the key is more than 256 entries back, and its last line is not read."""
    journal = FakeJournal([_BEGIN, *_BODY[:9], *_OTHER, *_BODY[9:20], "svc: done", _END])
    window = {"since": journal_time(310), "until": journal_time(321)}
    for params in (window, {"until": journal_time(321), "lines": 12}):
        answer = await _journal(journal, **params)
        _assert_hidden(answer, 12)
        assert _lines_of(answer)[-1] == "svc: done" and "11 of the lines" in answer["warning"]
        _assert_hidden(await _journal(journal, **params, filter="madeUpKeyBody"), 0)


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
    log.write_text(("x" * 65536 + "\n") * 256 + "the last\n")
    answer = await _linux_file(log, lines=1)
    assert _lines_of(answer) == ["the last"] and answer["truncated"] is True
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


async def test_file_key_that_starts_far_before_the_page_is_hidden(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("\n".join([_BEGIN, _BODY[0], *_OTHER, *_BODY[1:4], "svc: done"]) + "\n")
    answer = await _linux_file(log, lines=4)
    _assert_hidden(answer, 4)
    assert _lines_of(answer)[-1] == "svc: done"
    log.write_text("\n".join([_BEGIN, *_OTHER * 34, _BODY[0], "svc: done"]) + "\n")
    _assert_hidden(await _linux_file(log, filter="madeUpKeyBody"), 0)


async def test_file_lone_end_line_far_behind_the_body_hides_it(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("\n".join([*_BODY[:3], *_OTHER, _END]) + "\n")
    _assert_hidden(await _linux_file(log, lines=400), 304)


@pytest.mark.parametrize("awk", _AWKS)
async def test_file_lines_that_are_not_utf8_do_not_fail_the_read(tmp_path: Path, awk: str) -> None:
    log = tmp_path / "app.log"
    latin = "caf\u00e9 ".encode("latin-1")
    log.write_bytes(latin + _BEGIN.encode() + b"\n" + latin + b"\xc3\xc3\xa9\xa9\nlast one\n")
    # run_shell decodes as strictly as the SSH reader does.
    read, _ = _read_file(log, 1, awk)
    assert read.lines == ["last one"]
    assert read.context == [f"caf? {_BEGIN}", "caf? ????"]
    # Valid UTF-8 stays; what Python does not take as UTF-8 does not.
    valid = "\u00e9 \u20ac \U0001f600 tab\t."
    bad = [
        b"\xe0\x80\x80",
        b"\xed\xa0\x80",
        b"\xf4\x90\x80\x80",
        b"\xc0\xaf",
        b"\xe2\xc3\xa9\x82\xac",
    ]
    log.write_bytes(valid.encode() + b"\n" + b"\n".join(b"x " + part for part in bad) + b"\n")
    page, _ = _read_file(log, 10, awk)
    assert page.lines == [valid, *("x " + "?" * len(part) for part in bad)]


async def test_file_latin1_line_before_the_last_line_is_read(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_bytes(b"caf\xe9\nthe last\n")
    assert _lines_of(await _linux_file(log, lines=1)) == ["the last"]
    assert _lines_of(await _linux_file(log, lines=2)) == ["caf?", "the last"]


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
    assert read.lines == ["cr", "no newline"] and read.cut
    assert read.context[0] == "first"
    assert read.context[1].endswith(_BEGIN) and len(read.context[1]) < 4400
    assert out.isascii()
    whole, _ = _read_file(tmp_path / "app.log", 1, awk, max_bytes=100)
    assert whole.lines == ["no newline"] and len(whole.context) == 3
    log.write_text("".join(f"line {i}\n" for i in range(1000)))
    most, _ = _read_file(log, 10, awk)
    assert most.lines == [f"line {i}" for i in range(990, 1000)]
    assert most.context == [f"line {i}" for i in range(990 - 256, 990)]


@pytest.mark.parametrize("awk", _AWKS)
def test_file_byte_caps_drop_the_oldest_lines(tmp_path: Path, awk: str) -> None:
    log = tmp_path / "app.log"
    log.write_text("".join(f"line {i}\n" for i in range(10)))
    capped, _ = _read_file(log, 10, awk, max_bytes=14)
    assert capped.lines == ["line 8", "line 9"] and capped.cut
    assert len(capped.context) == 8
    # The host looked at 20 bytes: the first of them are inside a line.
    scanned, _ = _read_file(log, 10, awk, scan_bytes=20)
    assert scanned.lines == ["line 8", "line 9"] and scanned.cut


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


async def test_k8s_key_that_starts_far_before_the_page_is_hidden() -> None:
    lines = [_BEGIN, *_BODY[:9], *_OTHER, *_BODY[9:20], "svc: done", _END]
    log = FakePodLog([f"{_stamp(i)} {line}" for i, line in enumerate(lines)])
    window = {"since": _stamp(310), "until": _stamp(321)}
    newest = FakePodLog(log.lines[:-1])
    for source, params in ((log, window), (newest, {"lines": 12})):
        answer = await _k8s(source, **params)
        _assert_hidden(answer, 12)
        assert _lines_of(answer)[-1] == "svc: done" and "11 of the lines" in answer["warning"]
        _assert_hidden(await _k8s(source, **params, filter="madeUpKeyBody"), 0)


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


async def test_k8s_newest_line_over_the_byte_cap_is_an_error() -> None:
    """The line comes in many chunks, and no more than 1 MiB of it is ever held."""
    log = FakePodLog([f"{_stamp(0)} old", f"{_stamp(1)} {'x ' * 4 * _MIB}"])
    held: list[int] = []
    add = logs_read_source._LineBuffer.add

    def watched(buffer: Any, part: bytes) -> None:
        add(buffer, part)
        held.append(len(buffer._kept))

    with (
        patch.object(logs_read_source._LineBuffer, "add", watched),
        pytest.raises(LogReadError, match="newest log line is larger than 1 MiB"),
    ):
        await _k8s(log, lines=1)
    assert len(held) > 100 and max(held) <= _MIB + 64 * 1024


@pytest.mark.parametrize("extra", [0, 1])
async def test_k8s_newest_line_at_the_byte_cap_is_read(extra: int) -> None:
    text = "x " * (_MIB // 2) + "y" * extra
    log = FakePodLog([f"{_stamp(0)} {text}"])
    if extra:
        with pytest.raises(LogReadError, match="larger than 1 MiB"):
            await _k8s(log, lines=1)
        return
    answer = await _k8s(log, lines=1)
    assert answer["total"] == 1 and answer["rows"][0]["line_truncated"] is True


async def test_k8s_line_over_the_byte_cap_before_the_newest_lines() -> None:
    """The page ends behind it. A key mark in it, also over two chunks, still counts."""
    for start in (2 * _MIB, 2 * _MIB - 10, 2 * _MIB - 30):
        large = "- " * (start // 2) + _BEGIN + " -" * _MIB
        lines = [large, "open text", "more text"]
        log = FakePodLog([f"{_stamp(i)} {line}" for i, line in enumerate(lines)])
        answer = await _k8s(log, lines=5)
        assert answer["total"] == 2 and answer["truncated"] is True
        assert "text" not in json.dumps(answer["rows"])
        window = await _k8s(log, since=_stamp(1))
        assert window["total"] == 2 and "text" not in json.dumps(window["rows"])
