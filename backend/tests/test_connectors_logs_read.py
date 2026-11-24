# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the shared log read: ``linux.logs.read`` and ``k8s.logs.read``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_logs_read import k8s_logs_read
from meho_backplane.connectors.linux import LINUX_OPS, LinuxSshConnector
from meho_backplane.connectors.linux.ops_file import CONTENT_MARKER
from meho_backplane.connectors.linux.ops_inspect import LinuxInspectError
from meho_backplane.connectors.log_read import (
    LogReadError,
    build_log_answer,
    parse_since,
    parse_until,
)
from meho_backplane.redaction import redact
from meho_backplane.redaction.resolver import get_default_policy

_PASSWORD = "made-up-pw-4711"
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


def _file_output(lines: list[str], path: str = "/var/log/app.log") -> str:
    hex_path = (path + "\n").encode().hex()
    return f"{hex_path}\n100\n{CONTENT_MARKER}\n" + "\n".join(lines) + "\n"


def _journal_output(entries: list[tuple[int, str]]) -> str:
    rows = [
        json.dumps({"__REALTIME_TIMESTAMP": str(us), "MESSAGE": msg, "PRIORITY": "6"})
        for us, msg in entries
    ]
    return "\n".join(rows) + "\nMEHO_JOURNAL_RC=0\n"


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


def test_answer_cut_keeps_the_asked_end_and_says_truncated() -> None:
    rows = [{"time": f"2026-01-01T00:00:0{i}Z", "line": str(i)} for i in range(5)]
    newest = build_log_answer(rows, lines=2, keep="newest")
    assert [r["line"] for r in newest["rows"]] == ["3", "4"]
    assert newest["truncated"] is True
    assert newest["covered"] == {"from": "2026-01-01T00:00:03Z", "to": "2026-01-01T00:00:04Z"}
    oldest = build_log_answer(rows, lines=2, keep="oldest", continuation="marker")
    assert [r["line"] for r in oldest["rows"]] == ["0", "1"]
    assert oldest["continuation"] == "marker"


def test_answer_without_times_or_cut() -> None:
    answer = build_log_answer([{"time": None, "line": "a"}], lines=5, keep="newest")
    assert answer["covered"] is None
    assert answer["truncated"] is False
    assert answer["total"] == 1
    empty = build_log_answer([], lines=5, keep="newest")
    assert empty["rows"] == [] and empty["total"] == 0


# ---------------------------------------------------------------------------
# linux.logs.read
# ---------------------------------------------------------------------------


async def test_linux_file_read_returns_rows_without_times() -> None:
    result, cmd = await _linux(
        {"source": "/var/log/app.log", "lines": 10}, _file_output(["a", "b"])
    )
    assert "tail -n 10" in cmd
    assert result["rows"] == [{"time": None, "line": "a"}, {"time": None, "line": "b"}]
    assert result["covered"] is None
    assert result["truncated"] is False


async def test_linux_file_read_marks_truncated_when_window_is_full() -> None:
    result, _ = await _linux({"source": "/var/log/app.log", "lines": 2}, _file_output(["a", "b"]))
    assert result["truncated"] is True


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
    out = _journal_output([(1_790_000_000_000_000, "start"), (1_790_000_060_000_000, "stop")])
    result, cmd = await _linux(
        {"source": "nfs-server.service", "since": "2026-11-10T08:00:00Z", "lines": 50}, out
    )
    assert "-u nfs-server.service" in cmd
    assert "--since='2026-11-10 08:00:00 UTC'" in cmd
    assert [r["line"] for r in result["rows"]] == ["start", "stop"]
    assert result["covered"]["from"] < result["covered"]["to"]
    assert result["continuation"] is None


async def test_linux_whole_journal_has_no_unit_flag() -> None:
    _, cmd = await _linux({"source": "journal"}, _journal_output([(1_790_000_000_000_000, "x")]))
    assert " -u " not in cmd


async def test_linux_journal_span_becomes_exact_start_time() -> None:
    _, cmd = await _linux(
        {"source": "journal", "since": "15m"}, _journal_output([(1_790_000_000_000_000, "x")])
    )
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
    hidden = redact(result, get_default_policy()).redacted
    assert _PASSWORD not in json.dumps(hidden)
    assert hidden["rows"][0]["line"] == "fine"


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
    assert "container" not in inner and inner["tail"] == 200


async def test_k8s_since_reads_a_window_with_times_and_keeps_the_marker() -> None:
    page = {
        "lines": ["2026-11-10T08:00:00.1Z first", "2026-11-10T08:00:01.2Z second"],
        "truncated": True,
        "next_since_time": "2026-11-10T08:00:02.3Z",
    }
    result, inner = await _k8s({"source": "n/p", "since": "2026-11-10T08:00:00Z"}, page)
    assert inner["since_time"] == "2026-11-10T08:00:00Z" and "tail" not in inner
    assert result["rows"][0] == {"time": "2026-11-10T08:00:00.1Z", "line": "first"}
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
    hidden = redact(result, get_default_policy()).redacted
    assert _PASSWORD not in json.dumps(hidden)
