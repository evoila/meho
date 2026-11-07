# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""bind9 and Windows DNS put their A, AAAA and CNAME records on the topology map.

Covers the shared snapshot builder
(:mod:`meho_backplane.connectors._shared.dns_topology`) and both readers
(:mod:`meho_backplane.connectors.bind9.topology`,
:mod:`meho_backplane.connectors.windows_dns.topology`) against a mocked
``_run_command``: the facts and their source, the links, the zones that
are skipped (not allowed, missing, not text), and the failures that must
stop the refresh instead of looking like an empty zone.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.dns_topology import (
    DnsRecord,
    DnsZoneSkip,
    build_dns_topology_hints,
)
from meho_backplane.connectors._shared.pwsh import PwshRunError
from meho_backplane.connectors.bind9 import Bind9Connector
from meho_backplane.connectors.bind9.topology import Bind9TopologyReadError, zones_to_read
from meho_backplane.connectors.schemas import TopologyHints
from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector
from meho_backplane.connectors.windows_dns.topology import (
    WindowsDnsTopologyReadError,
    build_zone_records_script,
)
from meho_backplane.connectors.windows_dns.topology import (
    zones_to_read as windows_zones_to_read,
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
    name="dns-test", host="dns.test.invalid", port=22, secret_ref="meho/testing/dns"
)


def _operator() -> Operator:
    return Operator(
        sub="op-1",
        name="Op One",
        email=None,
        raw_jwt="token",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.OPERATOR,
    )


def _proc(stdout: Any = "", stderr: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


def _node(hints: TopologyHints, kind: str, name: str) -> Any:
    matches = [n for n in hints.nodes if n.kind == kind and n.name == name]
    assert len(matches) == 1, (kind, name, [(n.kind, n.name) for n in hints.nodes])
    return matches[0]


def _names(hints: TopologyHints, kind: str) -> list[str]:
    return [n.name for n in hints.nodes if n.kind == kind]


# ---------------------------------------------------------------------------
# Shared builder
# ---------------------------------------------------------------------------


def test_builder_groups_records_per_name_with_source() -> None:
    hints = build_dns_topology_hints(
        target_name="dns-test",
        source="bind9",
        records=[
            DnsRecord("WWW.Example.com.", "A", "192.0.2.10", "example.com", "internal"),
            DnsRecord("www.example.com", "AAAA", "2001:db8:0:0::10", "example.com", "internal"),
            DnsRecord("alias.example.com.", "CNAME", "WWW.example.com.", "example.com"),
        ],
        zones_read=[("example.com", "internal")],
        zones_skipped=[DnsZoneSkip(zone="secret.example.com", reason="not_allowed")],
    )
    www = _node(hints, "dns-record", "www.example.com")
    assert www.properties == {
        "source": "bind9",
        "records": [
            {"type": "A", "value": "192.0.2.10", "zone": "example.com", "view": "internal"},
            {"type": "AAAA", "value": "2001:db8::10", "zone": "example.com", "view": "internal"},
        ],
    }
    target = _node(hints, "target", "dns-test")
    assert target.properties["dns_source"] == "bind9"
    assert target.properties["zones_read"] == [{"zone": "example.com", "view": "internal"}]
    assert target.properties["zones_skipped"] == [
        {"zone": "secret.example.com", "reason": "not_allowed"}
    ]
    kinds = {(e.from_name, e.to_name, e.kind) for e in hints.edges}
    assert ("www.example.com", "dns-test", "belongs-to") in kinds
    assert ("alias.example.com", "www.example.com", "resolves-to") in kinds


def test_builder_links_by_name_address_and_short_name_only() -> None:
    hints = build_dns_topology_hints(
        target_name="dns-test",
        source="windows-dns",
        records=[DnsRecord("web01.example.com", "A", "192.0.2.10", "example.com")],
        zones_read=[("example.com", None)],
        zones_skipped=[],
    )
    links = [(link.match_names, dict(link.properties)) for link in hints.links]
    assert links == [
        (("web01.example.com",), {"matched_on": "name"}),
        (("192.0.2.10",), {"matched_on": "address"}),
        (("web01",), {"matched_on": "short-name"}),
    ]
    assert all(link.to_kinds == ("vm", "host", "node") for link in hints.links)
    # A link never becomes a system node: only the target and the DNS name.
    assert {n.kind for n in hints.nodes} == {"target", "dns-record"}


def test_builder_drops_wildcards_other_types_and_bad_values() -> None:
    hints = build_dns_topology_hints(
        target_name="dns-test",
        source="bind9",
        records=[
            DnsRecord("*.example.com.", "A", "192.0.2.99", "example.com"),
            DnsRecord("mail.example.com.", "MX", "10 mx.example.com.", "example.com"),
            DnsRecord("bad.example.com.", "A", "not-an-address", "example.com"),
            DnsRecord("mixed.example.com.", "A", "2001:db8::1", "example.com"),
            DnsRecord("", "A", "192.0.2.1", "example.com"),
            DnsRecord("ext.example.com.", "CNAME", "other.example.net.", "example.com"),
        ],
        zones_read=[("example.com", None)],
        zones_skipped=[],
    )
    assert _names(hints, "dns-record") == ["ext.example.com"]
    # The CNAME points outside the server's names: no edge to a missing node.
    assert [e.kind for e in hints.edges] == ["belongs-to"]


def test_builder_empty_server_has_only_the_target() -> None:
    hints = build_dns_topology_hints(
        target_name="dns-test", source="bind9", records=[], zones_read=[], zones_skipped=[]
    )
    assert [(n.kind, n.name) for n in hints.nodes] == [("target", "dns-test")]
    assert hints.edges == ()
    assert hints.links == ()


# ---------------------------------------------------------------------------
# bind9
# ---------------------------------------------------------------------------

_CHECKCONF = (
    'zone "example.com" {\n\ttype master;\n\tfile "/etc/bind/db.example.com";\n};\n'
    'zone "secret.example.com" {\n\ttype master;\n\tfile "/etc/bind/db.secret";\n};\n'
    'zone "2.0.192.in-addr.arpa" {\n\ttype master;\n\tfile "/etc/bind/db.192";\n};\n'
    'zone "fwd.example.com" {\n\ttype forward;\n};\n'
    'zone "." {\n\ttype hint;\n\tfile "/usr/share/dns/root.hints";\n};\n'
)
_ZONEFILE = (
    "$TTL 300\n"
    "@ IN SOA ns1 hostmaster 1 3600 600 86400 300\n"
    "@ IN NS ns1\n"
    "ns1 IN A 192.0.2.1\n"
    "web01 IN A 192.0.2.10\n"
    "www IN CNAME web01\n"
    "* IN A 192.0.2.99\n"
)


def _bind9_runner(responses: dict[str, Any]) -> AsyncMock:
    async def run(target: Any, cmd: str, **kwargs: Any) -> Any:
        del target, kwargs
        for prefix, response in responses.items():
            if cmd.startswith(prefix):
                if isinstance(response, BaseException):
                    raise response
                return response
        raise AssertionError(f"unexpected command {cmd!r}")

    return AsyncMock(side_effect=run)


def test_bind9_zones_to_read_keeps_forward_data_zones() -> None:
    from meho_backplane.connectors.bind9.ops_zone import parse_named_checkconf_zones

    rows = zones_to_read(parse_named_checkconf_zones(_CHECKCONF))
    assert [r["name"] for r in rows] == ["example.com", "secret.example.com"]


async def test_bind9_discover_reads_zones_and_skips_not_allowed() -> None:
    connector = Bind9Connector()
    run = _bind9_runner(
        {
            "named-checkconf -p": _proc(_CHECKCONF),
            "cat '/etc/bind/db.example.com'": _proc(_ZONEFILE),
            "cat '/etc/bind/db.secret'": _proc(
                "", "cat: /etc/bind/db.secret: Permission denied", 1
            ),
        }
    )
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=_operator())

    assert _names(hints, "dns-record") == [
        "ns1.example.com",
        "web01.example.com",
        "www.example.com",
    ]
    web = _node(hints, "dns-record", "web01.example.com")
    assert web.properties == {
        "source": "bind9",
        "records": [{"type": "A", "value": "192.0.2.10", "zone": "example.com"}],
    }
    target = _node(hints, "target", "dns-test")
    assert target.properties["zones_read"] == [{"zone": "example.com"}]
    assert target.properties["zones_skipped"] == [
        {"zone": "secret.example.com", "reason": "not_allowed"}
    ]
    # Only reads: named-checkconf and cat, never a write or sudo.
    commands = [c.args[1] for c in run.await_args_list]
    assert all(c.startswith(("named-checkconf -p", "cat ")) for c in commands)


async def test_bind9_missing_and_binary_zone_files_are_skipped() -> None:
    connector = Bind9Connector()
    run = _bind9_runner(
        {
            "named-checkconf -p": _proc(_CHECKCONF),
            "cat '/etc/bind/db.example.com'": UnicodeDecodeError("utf-8", b"\x00", 0, 1, "bad"),
            "cat '/etc/bind/db.secret'": _proc("", "cat: x: No such file or directory", 1),
        }
    )
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=_operator())
    assert _names(hints, "dns-record") == []
    assert _node(hints, "target", "dns-test").properties["zones_skipped"] == [
        {"zone": "example.com", "reason": "not_text"},
        {"zone": "secret.example.com", "reason": "file_missing"},
    ]


async def test_bind9_unparseable_zone_file_is_skipped() -> None:
    connector = Bind9Connector()
    checkconf = 'zone "example.com" {\n\ttype slave;\n\tfile "/var/cache/bind/db.ex";\n};\n'
    run = _bind9_runner(
        {
            "named-checkconf -p": _proc(checkconf),
            "cat ": _proc("this is not ( a zone file\n"),
        }
    )
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=_operator())
    assert _node(hints, "target", "dns-test").properties["zones_skipped"] == [
        {"zone": "example.com", "reason": "not_text"}
    ]


async def test_bind9_failed_checkconf_raises_instead_of_empty_map() -> None:
    connector = Bind9Connector()
    run = _bind9_runner({"named-checkconf -p": _proc("", "error", 1)})
    with patch.object(connector, "_run_command", run), pytest.raises(Bind9TopologyReadError):
        await connector.discover_topology(_TARGET, operator=_operator())


async def test_bind9_other_cat_failure_raises() -> None:
    connector = Bind9Connector()
    run = _bind9_runner(
        {
            "named-checkconf -p": _proc(_CHECKCONF),
            "cat '/etc/bind/db.example.com'": _proc("", "cat: Input/output error", 1),
        }
    )
    with patch.object(connector, "_run_command", run), pytest.raises(Bind9TopologyReadError):
        await connector.discover_topology(_TARGET, operator=_operator())


async def test_bind9_ssh_failure_raises() -> None:
    connector = Bind9Connector()
    run = _bind9_runner({"named-checkconf -p": TimeoutError()})
    with patch.object(connector, "_run_command", run), pytest.raises(TimeoutError):
        await connector.discover_topology(_TARGET, operator=_operator())


async def test_bind9_without_operator_uses_system_operator() -> None:
    connector = Bind9Connector()
    run = _bind9_runner({"named-checkconf -p": _proc("")})
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET)
    assert run.await_args.kwargs["operator"] is not None
    assert _names(hints, "dns-record") == []


# ---------------------------------------------------------------------------
# Windows DNS
# ---------------------------------------------------------------------------


def _script(cmd: str) -> str:
    return base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")


_ZONES = {
    "rows": [
        {"ZoneName": "example.com", "ZoneType": "Primary", "IsReverseLookupZone": False},
        {"ZoneName": "secret.example.com", "ZoneType": "Secondary"},
        {"ZoneName": "2.0.192.in-addr.arpa", "ZoneType": "Primary", "IsReverseLookupZone": True},
        {"ZoneName": "0.in-addr.arpa", "ZoneType": "Primary", "IsAutoCreated": True},
        {"ZoneName": "fwd.example.com", "ZoneType": "Forwarder"},
        {"ZoneName": "stub.example.com", "ZoneType": "Stub"},
    ],
    "total": 6,
}


def _windows_runner(per_zone: dict[str, Any]) -> AsyncMock:
    async def run(target: Any, cmd: str, **kwargs: Any) -> Any:
        del target, kwargs
        script = _script(cmd)
        if "Get-DnsServerZone" in script:
            return _proc(json.dumps(_ZONES))
        for zone, response in per_zone.items():
            if f"-ZoneName '{zone}'" in script:
                return _proc(json.dumps(response))
        raise AssertionError("unexpected script")

    return AsyncMock(side_effect=run)


def test_windows_zones_to_read_keeps_forward_primary_and_secondary() -> None:
    assert windows_zones_to_read(_ZONES["rows"]) == ["example.com", "secret.example.com"]


def test_windows_zone_script_quotes_the_zone_and_stops_on_error() -> None:
    script = build_zone_records_script("o'brien.example.com")
    assert "-ZoneName 'o''brien.example.com'" in script
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "PermissionDenied" in script
    assert "Remove-" not in script and "Add-" not in script and "Set-" not in script


async def test_windows_discover_reads_zones_and_skips_not_allowed() -> None:
    connector = WindowsDnsConnector()
    run = _windows_runner(
        {
            "example.com": {
                "ok": True,
                "rows": [
                    {"name": "@", "type": "A", "value": "192.0.2.5"},
                    {"name": "web01", "type": "A", "value": "192.0.2.10"},
                    {"name": "www", "type": "CNAME", "value": "web01.example.com."},
                ],
            },
            "secret.example.com": {"ok": False, "denied": True, "category": "PermissionDenied"},
        }
    )
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=_operator())

    assert _names(hints, "dns-record") == ["example.com", "web01.example.com", "www.example.com"]
    assert _node(hints, "dns-record", "www.example.com").properties == {
        "source": "windows-dns",
        "records": [{"type": "CNAME", "value": "web01.example.com", "zone": "example.com"}],
    }
    target = _node(hints, "target", "dns-test")
    assert target.properties["zones_read"] == [{"zone": "example.com"}]
    assert target.properties["zones_skipped"] == [
        {"zone": "secret.example.com", "reason": "not_allowed"}
    ]
    assert ("www.example.com", "web01.example.com", "resolves-to") in {
        (e.from_name, e.to_name, e.kind) for e in hints.edges
    }


async def test_windows_single_record_zone_is_read() -> None:
    """ConvertTo-Json renders a one-element list as a bare object."""
    connector = WindowsDnsConnector()
    run = _windows_runner(
        {
            "example.com": {
                "ok": True,
                "rows": {"name": "web01", "type": "A", "value": "192.0.2.10"},
            },
            "secret.example.com": {"ok": True, "rows": None},
        }
    )
    with patch.object(connector, "_run_command", run):
        hints = await connector.discover_topology(_TARGET, operator=_operator())
    assert _names(hints, "dns-record") == ["web01.example.com"]
    assert _node(hints, "target", "dns-test").properties["zones_skipped"] == []


async def test_windows_other_zone_error_raises_instead_of_empty_zone() -> None:
    connector = WindowsDnsConnector()
    run = _windows_runner(
        {
            "example.com": {"ok": False, "denied": False, "category": "ObjectNotFound"},
            "secret.example.com": {"ok": True, "rows": []},
        }
    )
    with (
        patch.object(connector, "_run_command", run),
        pytest.raises(WindowsDnsTopologyReadError, match="ObjectNotFound"),
    ):
        await connector.discover_topology(_TARGET, operator=_operator())


async def test_windows_failed_zone_list_raises() -> None:
    connector = WindowsDnsConnector()
    run = AsyncMock(return_value=_proc("", "denied", 1))
    with patch.object(connector, "_run_command", run), pytest.raises(PwshRunError):
        await connector.discover_topology(_TARGET, operator=_operator())
