# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``pfsense.openvpn.clients`` and ``pfsense.openvpn.log``.

* The status parser: columns come from the ``HEADER`` line, a server whose
  status did not end is ``readable: false`` (never an empty client list),
  and no server at all gives an empty list.
* The log parser: event types, the window, the year of a year-less stamp.
* The handlers: bad input is refused before any SSH call, the common name
  is quoted, a failed read raises, a missing log file raises, and the
  result is cut and flagged at the limit.
* The registration: both ops are ``safe`` reads in the ``openvpn`` group.

All names and addresses are made up (RFC 5737 / RFC 3849 ranges).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.pfsense.ops_openvpn import (
    classify_log_line,
    parse_openvpn_log,
    parse_openvpn_status,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget("pfsense-test", "pfsense.test.invalid", 22, "meho/testing/pfsense/x")


def _proc(stdout: str = "", exit_status: int = 0, stderr: str = "") -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


_HEADER = (
    "HEADER\tCLIENT_LIST\tCommon Name\tReal Address\tVirtual Address\t"
    "Virtual IPv6 Address\tBytes Received\tBytes Sent\tConnected Since\t"
    "Connected Since (time_t)\tUsername\tClient ID\tPeer ID\n"
)
_STATUS = (
    "@@SERVER server1\n@@CONF proto udp4\n@@CONF port 1194\n@@STATUS\n"
    ">INFO:OpenVPN Management Interface Version 5 -- type 'help' for more info\n"
    "TITLE\tOpenVPN 2.6.8\n"
    + _HEADER
    + "CLIENT_LIST\talice\t203.0.113.5:4000\t198.51.100.2\t\t1000\t2000\t"
    "Tue Nov 10 09:00:00 2026\t1789030800\talice\t3\t0\n"
    "END\n@@END\n"
    "@@SERVER server2\n@@CONF proto tcp4\n@@CONF port 443\n@@STATUS\n"
    "@@END\n"
)

# The firewall clock: 10 Nov 2026 12:00:00 at UTC+01:00.
_NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone(timedelta(hours=1)))
_DATE = "2026-11-10 12:00:00 +0100"


def test_status_parser_reads_columns_by_header_name() -> None:
    servers = parse_openvpn_status(_STATUS)
    first = servers[0]
    assert (first["server"], first["proto"], first["port"]) == ("server1", "udp4", "1194")
    assert first["readable"] is True
    assert first["clients"] == [
        {
            "common_name": "alice",
            "real_address": "203.0.113.5:4000",
            "virtual_address": "198.51.100.2",
            "virtual_ipv6_address": None,
            "bytes_received": 1000,
            "bytes_sent": 2000,
            "connected_since": datetime.fromtimestamp(1789030800, tz=UTC).isoformat(),
            "username": "alice",
        }
    ]


def test_status_parser_server_without_end_is_unreadable_not_empty() -> None:
    servers = parse_openvpn_status(_STATUS)
    # server2 printed nothing: unknown, not "no clients".
    assert servers[1]["readable"] is False
    assert servers[1]["clients"] == []


def test_status_parser_no_servers_and_odd_input() -> None:
    assert parse_openvpn_status("") == []
    assert parse_openvpn_status("garbage\nCLIENT_LIST\tx\n") == []


def test_status_parser_shorter_row_gives_nulls() -> None:
    out = "@@SERVER server1\n@@STATUS\n" + _HEADER + "CLIENT_LIST\tbob\n" + "END\n@@END\n"
    client = parse_openvpn_status(out)[0]["clients"][0]
    assert client["common_name"] == "bob"
    assert client["bytes_received"] is None
    assert client["connected_since"] is None


@pytest.mark.parametrize(
    ("message", "event", "reason"),
    [
        (
            "alice/203.0.113.5:4000 [alice] Peer Connection Initiated with [AF_INET]x",
            "connect",
            None,
        ),
        ("alice/203.0.113.5:4000 SIGUSR1[soft,ping-restart] received", "restart", "ping-restart"),
        (
            "alice/203.0.113.5:4000 [alice] Inactivity timeout (--inactive), exiting",
            "disconnect",
            "inactivity-timeout",
        ),
        ("alice/203.0.113.5:4000 SIGTERM[soft,remote-exit] received", "disconnect", "remote-exit"),
        ("alice/203.0.113.5:4000 TLS Error: TLS handshake failed", "error", None),
        ("alice/203.0.113.5:4000 MULTI: Learn: x", "other", None),
    ],
)
def test_classify_log_line(message: str, event: str, reason: str | None) -> None:
    assert classify_log_line(message) == {"event": event, "reason": reason}


_LOG = [
    "Nov 10 09:00:01 fw openvpn[1]: alice/203.0.113.5:4000 [alice] Peer Connection Initiated",
    "Nov 10 10:30:00 fw openvpn[1]: alice/203.0.113.5:4000 SIGUSR1[soft,ping-restart] received",
    "Nov 10 11:30:00 fw openvpn[1]: alice/203.0.113.5:4000 [alice] Peer Connection Initiated",
    "Dec 31 23:59:59 fw openvpn[1]: alice/203.0.113.5:4000 SIGTERM[soft,remote-exit] received",
    "not a syslog line alice",
]


def test_log_parser_window_filters_and_orders() -> None:
    since = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)  # 10:30 at the firewall
    until = datetime(2026, 11, 10, 10, 45, tzinfo=UTC)
    events = parse_openvpn_log(_LOG, _NOW, since, until)
    assert [e["event"] for e in events] == ["restart", "connect"]
    assert events[0]["time"] == "2026-11-10T10:30:00+01:00"


def test_log_parser_december_line_in_november_belongs_to_last_year() -> None:
    events = parse_openvpn_log([_LOG[3]], _NOW, None, None)
    assert events[0]["time"] == "2025-12-31T23:59:59+01:00"


def test_log_parser_unplaceable_line_only_without_window() -> None:
    assert parse_openvpn_log([_LOG[4]], _NOW, None, None)[0]["time"] is None
    assert parse_openvpn_log([_LOG[4]], _NOW, datetime(2020, 1, 1, tzinfo=UTC), None) == []


def test_log_parser_empty_input() -> None:
    assert parse_openvpn_log([], _NOW, None, None) == []


def _connector(proc: Any) -> tuple[PfSenseConnector, AsyncMock]:
    connector = PfSenseConnector()
    run = AsyncMock(return_value=proc)
    return connector, run


@pytest.mark.asyncio
async def test_clients_handler_returns_servers_and_count() -> None:
    connector, run = _connector(_proc(_STATUS))
    with patch.object(PfSenseConnector, "_run_command", run):
        result = await connector.openvpn_clients(_TARGET, {})
    assert result["client_count"] == 1
    assert [s["readable"] for s in result["servers"]] == [True, False]
    assert "nc -U" in run.call_args.args[1]


@pytest.mark.asyncio
async def test_clients_handler_failed_command_raises() -> None:
    connector, run = _connector(_proc("", exit_status=1, stderr="boom"))
    with (
        patch.object(PfSenseConnector, "_run_command", run),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await connector.openvpn_clients(_TARGET, {})


@pytest.mark.asyncio
async def test_clients_handler_no_servers_is_an_empty_list() -> None:
    connector, run = _connector(_proc(""))
    with patch.object(PfSenseConnector, "_run_command", run):
        result = await connector.openvpn_clients(_TARGET, {})
    assert result == {"servers": [], "client_count": 0}


@pytest.mark.asyncio
async def test_log_handler_happy_path_quotes_name() -> None:
    connector, run = _connector(_proc(_DATE + "\n" + "\n".join(_LOG[:3]) + "\n"))
    with patch.object(PfSenseConnector, "_run_command", run):
        result = await connector.openvpn_log(
            _TARGET, {"common_name": "alice", "since": "2026-11-10T09:30:00Z"}
        )
    assert [e["event"] for e in result["events"]] == ["restart", "connect"]
    assert result["event_count"] == 2
    assert result["truncated"] is False
    assert "grep -F -e alice /var/log/openvpn.log" in run.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"common_name": ""},
        {"common_name": "a"},
        {"common_name": "-v"},
        {"common_name": "alice; reboot"},
        {"common_name": "alice$(id)"},
        {"common_name": "alice", "since": "2026-11-10T09:00:00"},
        {"common_name": "alice", "since": "yesterday"},
        {"common_name": "alice", "since": "2026-11-11T00:00:00Z", "until": "2026-11-10T00:00:00Z"},
        {"common_name": "alice", "limit": 0},
        {"common_name": "alice", "limit": 1001},
        {"common_name": "alice", "limit": True},
    ],
)
async def test_log_handler_bad_input_is_refused_before_ssh(params: dict[str, Any]) -> None:
    connector, run = _connector(_proc())
    with patch.object(PfSenseConnector, "_run_command", run), pytest.raises(ValueError):
        await connector.openvpn_log(_TARGET, params)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_log_handler_no_matching_lines_is_empty_not_error() -> None:
    connector, run = _connector(_proc(_DATE + "\n"))
    with patch.object(PfSenseConnector, "_run_command", run):
        result = await connector.openvpn_log(_TARGET, {"common_name": "nobody"})
    assert result["events"] == []
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_log_handler_missing_log_file_raises() -> None:
    connector, run = _connector(_proc(_DATE + "\n@@NOLOG\n", exit_status=3))
    with (
        patch.object(PfSenseConnector, "_run_command", run),
        pytest.raises(RuntimeError, match="not readable"),
    ):
        await connector.openvpn_log(_TARGET, {"common_name": "alice"})


@pytest.mark.asyncio
async def test_log_handler_unreadable_date_raises() -> None:
    connector, run = _connector(_proc("oops\n" + _LOG[0] + "\n"))
    with (
        patch.object(PfSenseConnector, "_run_command", run),
        pytest.raises(RuntimeError, match="date"),
    ):
        await connector.openvpn_log(_TARGET, {"common_name": "alice"})


@pytest.mark.asyncio
async def test_log_handler_limit_keeps_newest_and_flags_truncated() -> None:
    connector, run = _connector(_proc(_DATE + "\n" + "\n".join(_LOG[:3]) + "\n"))
    with patch.object(PfSenseConnector, "_run_command", run):
        result = await connector.openvpn_log(_TARGET, {"common_name": "alice", "limit": 1})
    assert result["truncated"] is True
    assert len(result["events"]) == 1
    assert result["events"][0]["time"] == "2026-11-10T11:30:00+01:00"


@pytest.mark.asyncio
async def test_log_handler_full_tail_flags_truncated() -> None:
    lines = [_LOG[0]] * 5000
    connector, run = _connector(_proc(_DATE + "\n" + "\n".join(lines) + "\n"))
    with patch.object(PfSenseConnector, "_run_command", run):
        result = await connector.openvpn_log(_TARGET, {"common_name": "alice", "limit": 1000})
    assert result["truncated"] is True


def test_registration_both_ops_are_safe_reads_in_known_group() -> None:
    ops = {op.op_id: op for op in PFSENSE_OPS if op.op_id.startswith("pfsense.openvpn.")}
    assert set(ops) == {"pfsense.openvpn.clients", "pfsense.openvpn.log"}
    for op in ops.values():
        assert (op.safety_level, op.requires_approval) == ("safe", False)
        assert op.group_key == "openvpn"
        assert op.parameter_schema["additionalProperties"] is False
    assert "openvpn" in _WHEN_TO_USE_BY_GROUP
