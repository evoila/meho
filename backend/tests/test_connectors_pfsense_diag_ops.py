# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``pfsense.diag.ping``.

* Happy path: ``ifconfig -a`` is read, the source is one of its
  addresses, and the exact bounded ``ping`` command runs.
* IPv6 and host-name targets pick the right ``-4`` / ``-6`` flag.
* No reply (exit 2) still returns the summary with ``reachable=false``.
* A source that is not on the firewall is refused before ping runs.
* Bad input (shell text, names as source, a count out of range, mixed
  IP versions) is refused before any SSH call.
* A ping that prints no summary (unknown host) raises.
* The summary parser, the response schema and the registration.

All addresses are documentation ranges (RFC 5737 / RFC 3849).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.pfsense.ops_diag import (
    firewall_addresses,
    parse_ping_summary,
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


_TARGET = _StubTarget(
    name="pfsense-test",
    host="pfsense.test.invalid",
    port=22,
    secret_ref="meho/testing/pfsense/pfsense-test",
)


def _proc(stdout: str = "", exit_status: int = 0, stderr: str = "") -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


_IFCONFIG = (
    "vtnet0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
    "\tether 00:00:5e:00:53:01\n"
    "\tinet 192.0.2.1 netmask 0xffffff00 broadcast 192.0.2.255\n"
    "\tinet6 fe80::1%vtnet0 prefixlen 64 scopeid 0x1\n"
    "\tinet6 2001:db8:1::1 prefixlen 64\n"
    "\tstatus: active\n"
    "ovpns1: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> metric 0 mtu 1500\n"
    "\tinet 198.51.100.1 --> 198.51.100.2 netmask 0xffffff00\n"
)

_PING_OK = (
    "PING 203.0.113.10 (203.0.113.10) from 198.51.100.1: 56 data bytes\n"
    "64 bytes from 203.0.113.10: icmp_seq=0 ttl=63 time=0.633 ms\n"
    "64 bytes from 203.0.113.10: icmp_seq=1 ttl=63 time=0.412 ms\n"
    "64 bytes from 203.0.113.10: icmp_seq=2 ttl=63 time=0.459 ms\n"
    "\n"
    "--- 203.0.113.10 ping statistics ---\n"
    "3 packets transmitted, 3 packets received, 0.0% packet loss\n"
    "round-trip min/avg/max/stddev = 0.412/0.501/0.633/0.094 ms\n"
)

_PING_NO_REPLY = (
    "PING 203.0.113.10 (203.0.113.10) from 198.51.100.1: 56 data bytes\n"
    "\n"
    "--- 203.0.113.10 ping statistics ---\n"
    "3 packets transmitted, 0 packets received, 100.0% packet loss\n"
)

_PING6_OK = (
    "PING(56=40+8+8 bytes) 2001:db8:1::1 --> 2001:db8::10\n"
    "16 bytes from 2001:db8::10, icmp_seq=0 hlim=64 time=0.300 ms\n"
    "\n"
    "--- 2001:db8::10 ping statistics ---\n"
    "1 packets transmitted, 1 packets received, 0.0% packet loss\n"
    "round-trip min/avg/max/std-dev = 0.300/0.300/0.300/0.000 ms\n"
)


def _commands(mock_cmd: AsyncMock) -> list[str]:
    return [call.args[1] for call in mock_cmd.await_args_list]


async def test_ping_happy_path_runs_bounded_command() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_IFCONFIG), _proc(_PING_OK)]
        result = await connector.diag_ping(
            _TARGET, {"host": "203.0.113.10", "source": "198.51.100.1"}
        )

    assert result == {
        "host": "203.0.113.10",
        "source": "198.51.100.1",
        "count": 3,
        "packets_sent": 3,
        "packets_received": 3,
        "packet_loss_percent": 0.0,
        "rtt_ms": {"min": 0.412, "avg": 0.501, "max": 0.633},
        "reachable": True,
    }
    assert _commands(mock_cmd) == [
        "ifconfig -a",
        "ping -4 -n -c 3 -W 1000 -t 5 -S 198.51.100.1 203.0.113.10",
    ]
    assert mock_cmd.await_args_list[1].kwargs["timeout"] == 18.0


async def test_ping_ipv6_uses_dash_six() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_IFCONFIG), _proc(_PING6_OK)]
        result = await connector.diag_ping(
            _TARGET, {"host": "2001:db8::10", "source": "2001:db8:1::1", "count": 1}
        )
    assert result["reachable"] is True
    assert result["rtt_ms"] == {"min": 0.3, "avg": 0.3, "max": 0.3}
    assert _commands(mock_cmd)[1] == "ping -6 -n -c 1 -W 1000 -t 3 -S 2001:db8:1::1 2001:db8::10"


async def test_ping_host_name_follows_source_version() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_IFCONFIG), _proc(_PING_OK)]
        result = await connector.diag_ping(
            _TARGET, {"host": "host-1.example.com", "source": "192.0.2.1", "count": 5}
        )
    assert result["host"] == "host-1.example.com"
    assert _commands(mock_cmd)[1] == (
        "ping -4 -n -c 5 -W 1000 -t 7 -S 192.0.2.1 host-1.example.com"
    )


async def test_ping_no_reply_is_a_result_not_an_error() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_IFCONFIG), _proc(_PING_NO_REPLY, exit_status=2)]
        result = await connector.diag_ping(
            _TARGET, {"host": "203.0.113.10", "source": "198.51.100.1"}
        )
    assert result["reachable"] is False
    assert result["packets_received"] == 0
    assert result["packet_loss_percent"] == 100.0
    assert result["rtt_ms"] is None


async def test_ping_refuses_source_not_on_the_firewall() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc(_IFCONFIG)]
        with pytest.raises(ValueError, match="not an address of this firewall"):
            await connector.diag_ping(_TARGET, {"host": "203.0.113.10", "source": "192.0.2.99"})
    # Only ifconfig ran; ping never did.
    assert _commands(mock_cmd) == ["ifconfig -a"]


async def test_ping_without_summary_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            _proc(_IFCONFIG),
            _proc("", exit_status=68, stderr="ping: cannot resolve nohost.example.com\n"),
        ]
        with pytest.raises(RuntimeError, match="exit 68: ping: cannot resolve"):
            await connector.diag_ping(
                _TARGET, {"host": "nohost.example.com", "source": "198.51.100.1"}
            )


async def test_ping_ifconfig_failure_raises() -> None:
    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [_proc("", exit_status=1)]
        with pytest.raises(RuntimeError, match="ifconfig"):
            await connector.diag_ping(_TARGET, {"host": "203.0.113.10", "source": "198.51.100.1"})


@pytest.mark.parametrize(
    "params",
    [
        {"host": "203.0.113.10; reboot", "source": "198.51.100.1"},
        {"host": "$(reboot)", "source": "198.51.100.1"},
        {"host": "-f", "source": "198.51.100.1"},
        {"host": "a b", "source": "198.51.100.1"},
        {"host": "", "source": "198.51.100.1"},
        {"host": "224.0.0.1", "source": "198.51.100.1"},
        {"host": "10.1", "source": "198.51.100.1"},
        {"host": "167772161", "source": "198.51.100.1"},
        {"host": "0x7f000001", "source": "198.51.100.1"},
        {"host": "fe80::1%vtnet0", "source": "fe80::1"},
        {"host": "203.0.113.10", "source": "pfsense.example.com"},
        {"host": "203.0.113.10", "source": "0.0.0.0"},
        {"host": "203.0.113.10", "source": "2001:db8:1::1"},
        {"host": "203.0.113.10", "source": "198.51.100.1", "count": 6},
        {"host": "203.0.113.10", "source": "198.51.100.1", "count": 0},
        {"host": "203.0.113.10", "source": "198.51.100.1", "count": True},
        {"host": "203.0.113.10"},
        {"source": "198.51.100.1"},
    ],
)
async def test_ping_rejects_bad_input_before_ssh(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.diag_ping(_TARGET, params)
    assert mock_cmd.await_count == 0


def test_firewall_addresses_reads_ipv4_ipv6_and_point_to_point() -> None:
    found = {str(a) for a in firewall_addresses(_IFCONFIG)}
    assert found == {"192.0.2.1", "198.51.100.1", "fe80::1", "2001:db8:1::1"}


def test_parse_ping_summary_with_duplicates() -> None:
    out = (
        "--- 203.0.113.10 ping statistics ---\n"
        "3 packets transmitted, 3 packets received, +1 duplicates, 0.0% packet loss\n"
        "round-trip min/avg/max/stddev = 1.000/2.000/3.000/0.500 ms\n"
    )
    summary = parse_ping_summary(out)
    assert summary is not None
    assert summary["packets_received"] == 3
    assert summary["packet_loss_percent"] == 0.0
    assert summary["rtt_ms"] == {"min": 1.0, "avg": 2.0, "max": 3.0}
    assert parse_ping_summary("ping: unknown host") is None


def test_ping_response_schema_accepts_both_outcomes() -> None:
    from jsonschema import Draft202012Validator

    op = next(o for o in PFSENSE_OPS if o.op_id == "pfsense.diag.ping")
    assert op.response_schema is not None
    validator = Draft202012Validator(op.response_schema)
    base = {"host": "203.0.113.10", "source": "198.51.100.1", "count": 3, "packets_sent": 3}
    validator.validate(
        {
            **base,
            "packets_received": 3,
            "packet_loss_percent": 0.0,
            "reachable": True,
            "rtt_ms": {"min": 0.4, "avg": 0.5, "max": 0.6},
        }
    )
    validator.validate(
        {
            **base,
            "packets_received": 0,
            "packet_loss_percent": 100.0,
            "reachable": False,
            "rtt_ms": None,
        }
    )


def test_ping_is_a_safe_read_in_the_diagnostics_group() -> None:
    op = next(o for o in PFSENSE_OPS if o.op_id == "pfsense.diag.ping")
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == "diagnostics"
    assert "read-only" in op.tags
    assert op.parameter_schema["additionalProperties"] is False
    assert "diagnostics" in _WHEN_TO_USE_BY_GROUP
