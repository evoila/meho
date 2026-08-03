# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the windows_dns connector (SSH → PowerShell / DnsServer).

Mirrors the bind9 reads test harness (``test_connectors_bind9_reads.py``)
and the Holodeck pwsh test (``test_connectors_holodeck_pwsh.py``): a
``_StubTarget`` dataclass, a ``_completed_process`` SSHCompletedProcess
stub, and ``patch.object(connector, "_run_command", AsyncMock(...))`` so
the DnsServer-cmdlet handlers can be exercised without a real Windows
host. Because the transport is ``powershell -EncodedCommand
<base64-utf16le>`` (Windows PowerShell 5.1 -- ``pwsh``/PS7 is absent on a
Windows AD-DNS host by default), the assertions decode the base64 payload
back to the PowerShell script and assert on the cmdlet + quoted arguments
the handler built.

Coverage:

* the ``_pwsh`` encode helper round-trips the documented convention;
* ``windns.record.get`` builds ``Get-DnsServerResourceRecord`` with the
  quoted zone / name / RRType and parses the sample JSON rows (plus the
  empty-match and single-quote-injection cases);
* ``windns.record.add`` builds ``Add-DnsServerResourceRecordA`` /
  ``...CName`` and returns the write envelope (plus TTL rendering and the
  non-IPv4 rejection);
* ``windns.record.remove`` builds the ``-Force`` remove command;
* the connector's registry-v2 triple round-trips (the invariant the
  registry enforces at import time), and the op set carries the expected
  safety levels;
* ``windns.about`` maps the fingerprint into the flat identity dict.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meho_backplane.connectors.windows_dns  # noqa: F401 -- registers connector at import
from meho_backplane.connectors.registry import product_impl_id_round_trips
from meho_backplane.connectors.windows_dns import WINDOWS_DNS_OPS, WindowsDnsConnector
from meho_backplane.connectors.windows_dns._pwsh import encode_pwsh_command
from meho_backplane.connectors.windows_dns.ops_record import (
    ps_single_quote,
    windows_dns_record_add,
    windows_dns_record_get,
    windows_dns_record_remove,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires (mirrors the bind9 suite)."""
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
    # A Vault KV-v2 path STRING (#2155). The connector's ``_run_command``
    # is mocked in this suite, so the path is never resolved.
    secret_ref: str


_TARGET = _StubTarget(
    name="windns-test",
    host="dns.test.invalid",
    port=22,
    secret_ref="meho/testing/windows_dns/windns-test",
)


def _completed_process(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    """Stub mimicking asyncssh's :class:`SSHCompletedProcess`."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _script_from_call(run_mock: AsyncMock) -> str:
    """Recover the PowerShell script from the mocked ``_run_command`` argv.

    ``pwsh_run`` builds ``powershell -NoProfile -NonInteractive
    -EncodedCommand <base64-utf16le>`` and passes it as the 2nd positional
    arg to ``_run_command``. The executable is ``powershell`` (Windows
    PowerShell 5.1), **not** ``pwsh`` -- a Windows AD-DNS host lacks PS7 by
    default (verified against a live WS2022 DC). Decode the base64 tail
    back to the script text so assertions read against the cmdlet the
    handler composed; the decoded script carries the transport's prepended
    ``$ProgressPreference = 'SilentlyContinue';`` guard.
    """
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    encoded = cmd.rsplit(" ", 1)[1]
    return base64.b64decode(encoded).decode("utf-16-le")


async def test_transport_uses_windows_powershell_and_suppresses_progress() -> None:
    """The transport invokes ``powershell`` (not ``pwsh``) and prepends the
    ``$ProgressPreference`` guard so the DnsServer module's first-use
    progress never pollutes the JSON stream. Both are live-validated
    behaviours against a real WS2022 DC (pwsh absent; CLIXML progress on
    first module load).
    """
    connector = WindowsDnsConnector()
    assert connector.POWERSHELL_EXECUTABLE == "powershell"
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"rows":[],"total":0}'))
    with patch.object(connector, "_run_command", run_mock):
        await windows_dns_record_get(connector, _TARGET, {"zone": "evba.lab"})
    cmd: str = run_mock.await_args.args[1]
    assert cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    assert not cmd.startswith("pwsh ")
    script = _script_from_call(run_mock)
    assert script.startswith("$ProgressPreference = 'SilentlyContinue';")


# ---------------------------------------------------------------------------
# _pwsh encode helper
# ---------------------------------------------------------------------------


def test_encode_pwsh_command_round_trips_utf16le_base64() -> None:
    script = "Get-DnsServerZone | ConvertTo-Json"
    encoded = encode_pwsh_command(script)
    assert encoded == base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    assert base64.b64decode(encoded).decode("utf-16-le") == script
    # No UTF-16-LE BOM on the encoded payload (the #371 convention).
    assert not base64.b64decode(encoded).startswith(b"\xff\xfe")


def test_ps_single_quote_doubles_embedded_quotes() -> None:
    assert ps_single_quote("evba.lab") == "'evba.lab'"
    assert ps_single_quote("o'brien") == "'o''brien'"


# ---------------------------------------------------------------------------
# record.get
# ---------------------------------------------------------------------------


async def test_record_get_builds_expected_pwsh_command_and_parses_rows() -> None:
    connector = WindowsDnsConnector()
    sample = {
        "rows": [
            {
                "HostName": "www",
                "RecordType": "A",
                "RecordData": {"IPv4Address": "10.5.50.2"},
            }
        ],
        "total": 1,
    }
    run_mock = AsyncMock(return_value=_completed_process(stdout=json.dumps(sample)))
    with patch.object(connector, "_run_command", run_mock):
        result = await windows_dns_record_get(
            connector, _TARGET, {"zone": "evba.lab", "name": "www", "type": "A"}
        )
    script = _script_from_call(run_mock)
    assert "Get-DnsServerResourceRecord" in script
    assert "-ZoneName 'evba.lab'" in script
    assert "-Name 'www'" in script
    assert "-RRType 'A'" in script
    assert "ConvertTo-Json" in script
    assert result["zone"] == "evba.lab"
    assert result["type"] == "A"
    assert result["total"] == 1
    assert result["rows"][0]["RecordData"]["IPv4Address"] == "10.5.50.2"


async def test_record_get_omits_optional_filters_when_absent() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"rows":[],"total":0}'))
    with patch.object(connector, "_run_command", run_mock):
        await windows_dns_record_get(connector, _TARGET, {"zone": "evba.lab"})
    script = _script_from_call(run_mock)
    assert "-ZoneName 'evba.lab'" in script
    assert "-Name" not in script
    assert "-RRType" not in script


async def test_record_get_returns_empty_rows_for_no_match() -> None:
    connector = WindowsDnsConnector()
    # The hashtable envelope keeps stdout JSON-shaped even for a
    # zero-match read; the handler surfaces empty rows, not an error.
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"rows":null,"total":0}'))
    with patch.object(connector, "_run_command", run_mock):
        result = await windows_dns_record_get(
            connector, _TARGET, {"zone": "evba.lab", "name": "missing"}
        )
    assert result["rows"] == []
    assert result["total"] == 0


async def test_record_get_escapes_single_quote_in_zone() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"rows":[],"total":0}'))
    with patch.object(connector, "_run_command", run_mock):
        await windows_dns_record_get(connector, _TARGET, {"zone": "o'brien.lab"})
    script = _script_from_call(run_mock)
    # The embedded single quote is doubled inside the PowerShell literal.
    assert "-ZoneName 'o''brien.lab'" in script


async def test_record_get_rejects_unsupported_type() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"rows":[],"total":0}'))
    with (
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ValueError, match="unsupported record type"),
    ):
        await windows_dns_record_get(connector, _TARGET, {"zone": "evba.lab", "type": "BOGUS"})


# ---------------------------------------------------------------------------
# record.add
# ---------------------------------------------------------------------------


async def test_record_add_a_builds_cmdlet_and_returns_write_envelope() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"ok":true}'))
    with patch.object(connector, "_run_command", run_mock):
        result = await windows_dns_record_add(
            connector, _TARGET, {"zone": "evba.lab", "name": "api", "ip": "10.5.50.9"}
        )
    script = _script_from_call(run_mock)
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "Add-DnsServerResourceRecordA" in script
    assert "-ZoneName 'evba.lab'" in script
    assert "-Name 'api'" in script
    assert "-IPv4Address '10.5.50.9'" in script
    assert result == {
        "zone": "evba.lab",
        "name": "api",
        "type": "A",
        "value": "10.5.50.9",
        "ttl": None,
        "op_class": "write",
    }


async def test_record_add_a_with_ttl_renders_timespan() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"ok":true}'))
    with patch.object(connector, "_run_command", run_mock):
        result = await windows_dns_record_add(
            connector,
            _TARGET,
            {"zone": "evba.lab", "name": "api", "ip": "10.5.50.9", "ttl": 3600},
        )
    script = _script_from_call(run_mock)
    assert "-TimeToLive (New-TimeSpan -Seconds 3600)" in script
    assert result["ttl"] == 3600


async def test_record_add_cname_uses_cname_cmdlet() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"ok":true}'))
    with patch.object(connector, "_run_command", run_mock):
        result = await windows_dns_record_add(
            connector,
            _TARGET,
            {"zone": "evba.lab", "name": "alias", "type": "CNAME", "target": "www.evba.lab"},
        )
    script = _script_from_call(run_mock)
    assert "Add-DnsServerResourceRecordCName" in script
    assert "-HostNameAlias 'www.evba.lab'" in script
    assert result["type"] == "CNAME"
    assert result["value"] == "www.evba.lab"


async def test_record_add_a_rejects_non_ipv4() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"ok":true}'))
    with patch.object(connector, "_run_command", run_mock), pytest.raises(ValueError):
        await windows_dns_record_add(
            connector, _TARGET, {"zone": "evba.lab", "name": "api", "ip": "not-an-ip"}
        )
    # A rejected value never reaches the wire.
    run_mock.assert_not_awaited()


async def test_record_add_cname_requires_target() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"ok":true}'))
    with (
        patch.object(connector, "_run_command", run_mock),
        pytest.raises(ValueError, match="target"),
    ):
        await windows_dns_record_add(
            connector, _TARGET, {"zone": "evba.lab", "name": "alias", "type": "CNAME"}
        )


# ---------------------------------------------------------------------------
# record.remove
# ---------------------------------------------------------------------------


async def test_record_remove_builds_force_command() -> None:
    connector = WindowsDnsConnector()
    run_mock = AsyncMock(return_value=_completed_process(stdout='{"ok":true}'))
    with patch.object(connector, "_run_command", run_mock):
        result = await windows_dns_record_remove(
            connector, _TARGET, {"zone": "evba.lab", "name": "api", "type": "A"}
        )
    script = _script_from_call(run_mock)
    assert "Remove-DnsServerResourceRecord" in script
    assert "-ZoneName 'evba.lab'" in script
    assert "-Name 'api'" in script
    assert "-RRType 'A'" in script
    assert "-Force" in script
    assert result == {"zone": "evba.lab", "name": "api", "type": "A", "op_class": "write"}


# ---------------------------------------------------------------------------
# about (fingerprint wrapper)
# ---------------------------------------------------------------------------


async def test_about_maps_fingerprint_into_identity_dict() -> None:
    connector = WindowsDnsConnector()
    payload = {
        "Hostname": "DNS01",
        "DnsServerModulePresent": True,
        "DnsServerModuleVersion": "2.0.0.0",
    }
    run_mock = AsyncMock(return_value=_completed_process(stdout=json.dumps(payload)))
    with patch.object(connector, "_run_command", run_mock):
        result = await connector.about(_TARGET, {})
    assert result["vendor"] == "microsoft"
    assert result["product"] == "windows-dns"
    assert result["version"] == "2.0.0.0"
    assert result["hostname"] == "DNS01"
    assert result["dnsserver_module_present"] is True


# ---------------------------------------------------------------------------
# Registration / metadata invariants
# ---------------------------------------------------------------------------


def test_connector_id_triple_round_trips() -> None:
    # The successful import of the package at module top already exercised
    # register_connector_v2's round-trip guard; assert the invariant
    # directly so a future rename that breaks it fails loudly here too.
    assert WindowsDnsConnector.product == "windns"
    assert WindowsDnsConnector.version == "2016.x"
    assert WindowsDnsConnector.impl_id == "windns-ssh"
    assert product_impl_id_round_trips(product="windns", version="2016.x", impl_id="windns-ssh")


def test_op_surface_ids_and_safety_levels() -> None:
    by_id = {op.op_id: op for op in WINDOWS_DNS_OPS}
    assert set(by_id) == {
        "windns.about",
        "windns.zone.list",
        "windns.record.get",
        "windns.record.add",
        "windns.record.remove",
    }
    assert by_id["windns.about"].safety_level == "safe"
    assert by_id["windns.zone.list"].safety_level == "safe"
    assert by_id["windns.record.get"].safety_level == "safe"
    assert by_id["windns.record.add"].safety_level == "caution"
    assert by_id["windns.record.remove"].safety_level == "caution"
    # Every op declares a curated when_to_use group the registration walk
    # can resolve (identity / zone / record).
    assert {op.group_key for op in WINDOWS_DNS_OPS} == {"identity", "zone", "record"}
