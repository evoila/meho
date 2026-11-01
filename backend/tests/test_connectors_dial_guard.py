# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared connect-time address check (``dial_guard``).

Every driver checks the target address again when it connects, and dials
only a checked IP address:

* the shared check itself (IP literal, resolved name, allowlisted name,
  name that does not resolve);
* the HTTP pinned transport, SSH, Postgres, MSSQL, MongoDB, Kubernetes,
  the vSphere Supervisor login and the OVF upload client.

The suite-wide fixture in ``conftest.py`` lets names that do not resolve
be dialed by name and allowlists the private ranges. The ``_live`` fixture
here clears the allowlist, installs a fake resolver and restores the
refusal of names that do not resolve, so the real rules run.
"""

from __future__ import annotations

import ipaddress
import socket
import types
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpcore
import httpx
import pytest
from kubernetes_asyncio import client as k8s_client

from meho_backplane.connectors._shared import dial_guard
from meho_backplane.connectors._shared.dial_guard import (
    DialHostUnresolvedError,
    ScreeningResolver,
    dial_host,
    screen_dial_addresses,
)
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.adapters.http import SsrfBlockedError, _pin_target_addresses
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.kubernetes._dial_check import check_api_client_dials
from meho_backplane.connectors.kubernetes.wcp import wcp_login
from meho_backplane.connectors.mongodb import session as mongo_session
from meho_backplane.connectors.mssql import session as mssql_session
from meho_backplane.connectors.postgres import session as pg_session
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.connectors.vmware_rest import ovf_upload_tls
from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
)

# Captured at import, before the suite-wide fixture replaces it.
_REAL_UNRESOLVED = dial_guard._unresolved

_PUBLIC_IP = "93.184.216.34"
_PUBLIC_IP_2 = "93.184.216.35"

#: Fake DNS: name -> answers. A name missing here does not resolve.
_DNS: dict[str, list[str]] = {
    "public.example.test": [_PUBLIC_IP, _PUBLIC_IP_2],
    "private.example.test": ["10.0.0.7"],
    "metadata.example.test": ["169.254.169.254"],
    "mixed.example.test": [_PUBLIC_IP, "127.0.0.1"],
}


def _fake_resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return [ipaddress.ip_address(a) for a in _DNS.get(host.rstrip(".").lower(), [])]


@pytest.fixture
def _live(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv(TARGET_SSRF_ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", _fake_resolve)
    monkeypatch.setattr(dial_guard, "_unresolved", _REAL_UNRESOLVED)
    return monkeypatch


def _target(host: str, **extra: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        name="t1",
        host=host,
        port=None,
        secret_ref=None,
        tenant_id=uuid.uuid4(),
        id=uuid.uuid4(),
        **extra,
    )


# ---------------------------------------------------------------------------
# The shared check
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_live")
def test_public_ip_literal_is_returned_as_is() -> None:
    assert screen_dial_addresses(_PUBLIC_IP) == [_PUBLIC_IP]


@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1"])
def test_blocked_ip_literal_is_refused(host: str) -> None:
    with pytest.raises(TargetDestinationBlockedError):
        screen_dial_addresses(host)


@pytest.mark.usefixtures("_live")
def test_public_name_returns_every_checked_answer() -> None:
    assert screen_dial_addresses("public.example.test") == [_PUBLIC_IP, _PUBLIC_IP_2]


@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize(
    "host", ["private.example.test", "metadata.example.test", "mixed.example.test"]
)
def test_name_with_a_blocked_answer_is_refused(host: str) -> None:
    with pytest.raises(TargetDestinationBlockedError) as excinfo:
        screen_dial_addresses(host)
    # The message never names the resolved address.
    for answer in _DNS[host]:
        assert answer not in str(excinfo.value)


@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize("host", ["gone.example.test", "", "   "])
def test_name_that_does_not_resolve_is_refused(host: str) -> None:
    with pytest.raises(DialHostUnresolvedError):
        screen_dial_addresses(host)


def test_allowlisted_name_is_dialed_by_name(_live: pytest.MonkeyPatch) -> None:
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "gone.example.test")
    assert screen_dial_addresses("gone.example.test") is None


def test_allowlisted_range_lets_a_private_answer_through(_live: pytest.MonkeyPatch) -> None:
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "10.0.0.0/8")
    assert screen_dial_addresses("private.example.test") == ["10.0.0.7"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_dial_host_returns_the_first_checked_address() -> None:
    assert await dial_host("public.example.test") == _PUBLIC_IP


@pytest.mark.asyncio
async def test_dial_host_keeps_an_allowlisted_name(_live: pytest.MonkeyPatch) -> None:
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "gone.example.test")
    assert await dial_host("gone.example.test") == "gone.example.test"


# ---------------------------------------------------------------------------
# HTTP pinned transport
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_live")
def test_http_pin_refuses_a_name_that_does_not_resolve() -> None:
    with pytest.raises(SsrfBlockedError):
        _pin_target_addresses("gone.example.test")


@pytest.mark.usefixtures("_live")
def test_http_pin_returns_checked_addresses() -> None:
    assert _pin_target_addresses("public.example.test") == [_PUBLIC_IP, _PUBLIC_IP_2]


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


class _Ssh(SshConnector):
    product = "test-ssh"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


def _ssh_with_fake_connect(monkeypatch: pytest.MonkeyPatch) -> tuple[_Ssh, AsyncMock]:
    conn = MagicMock()
    conn.is_closed.return_value = False
    fake_connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(ssh_module.asyncssh, "connect", fake_connect)
    connector = _Ssh()
    monkeypatch.setattr(
        connector,
        "_auth_config",
        AsyncMock(return_value={"username": "u", "password": "p", "known_hosts": None}),
    )
    return connector, fake_connect


@pytest.mark.asyncio
async def test_ssh_dials_the_checked_ip_and_matches_the_host_key_by_name(
    _live: pytest.MonkeyPatch,
) -> None:
    connector, fake_connect = _ssh_with_fake_connect(_live)
    await connector._connect(_target("public.example.test"))
    args, kwargs = fake_connect.call_args
    assert args == (_PUBLIC_IP,)
    assert kwargs["host_key_alias"] == "public.example.test"


@pytest.mark.asyncio
async def test_ssh_ip_target_needs_no_host_key_alias(_live: pytest.MonkeyPatch) -> None:
    connector, fake_connect = _ssh_with_fake_connect(_live)
    await connector._connect(_target(_PUBLIC_IP))
    args, kwargs = fake_connect.call_args
    assert args == (_PUBLIC_IP,)
    assert "host_key_alias" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["private.example.test", "gone.example.test"])
async def test_ssh_refuses_before_connecting(_live: pytest.MonkeyPatch, host: str) -> None:
    connector, fake_connect = _ssh_with_fake_connect(_live)
    with pytest.raises(TargetDestinationBlockedError):
        await connector._connect(_target(host))
    fake_connect.assert_not_called()


# ---------------------------------------------------------------------------
# Postgres and MSSQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_dials_the_checked_ip(_live: pytest.MonkeyPatch) -> None:
    fake_connect = AsyncMock(return_value=MagicMock())
    _live.setattr(pg_session.asyncpg, "connect", fake_connect)
    await pg_session.connect_read_only(_target("public.example.test"), None)
    assert fake_connect.call_args.kwargs["host"] == _PUBLIC_IP


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["metadata.example.test", "gone.example.test"])
async def test_postgres_refuses_before_connecting(_live: pytest.MonkeyPatch, host: str) -> None:
    fake_connect = AsyncMock()
    _live.setattr(pg_session.asyncpg, "connect", fake_connect)
    with pytest.raises(TargetDestinationBlockedError):
        await pg_session.connect_read_only(_target(host), None)
    fake_connect.assert_not_called()


@pytest.mark.asyncio
async def test_mssql_dials_the_checked_ip(_live: pytest.MonkeyPatch) -> None:
    fake_fetch = MagicMock(return_value=[])
    _live.setattr(mssql_session, "_blocking_fetch", fake_fetch)
    _live.setattr(
        mssql_session,
        "resolve_sql_credentials",
        AsyncMock(return_value={"sql_username": "u", "sql_password": "p"}),
    )
    await mssql_session.fetch_rows(_target("public.example.test"), None, "SELECT 1")
    assert fake_fetch.call_args.kwargs["host"] == _PUBLIC_IP


@pytest.mark.asyncio
async def test_mssql_refuses_before_connecting(_live: pytest.MonkeyPatch) -> None:
    fake_execute = MagicMock()
    _live.setattr(mssql_session, "_blocking_execute", fake_execute)
    _live.setattr(
        mssql_session,
        "resolve_sql_credentials",
        AsyncMock(return_value={"sql_username": "u", "sql_password": "p"}),
    )
    with pytest.raises(TargetDestinationBlockedError):
        await mssql_session.execute_statement(_target("private.example.test"), None, "SELECT 1")
    fake_execute.assert_not_called()


# ---------------------------------------------------------------------------
# MongoDB: the client reaches only the checked host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mongodb_client_knows_only_the_checked_address(_live: pytest.MonkeyPatch) -> None:
    """The real client is built (no I/O) and its topology holds one checked seed.

    A direct connection to one IP address means the driver makes no SRV
    lookup and adds no replica-set members it learns from the server.
    """
    mongo = await mongo_session.connect_client(_target("public.example.test"), None)
    try:
        description = mongo.topology_description
        assert description.topology_type_name == "Single"
        assert list(description.server_descriptions()) == [(_PUBLIC_IP, mongo_session.DEFAULT_PORT)]
    finally:
        await mongo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "private.example.test",
        "gone.example.test",
        "mongodb+srv://public.example.test",
        "public.example.test,private.example.test",
    ],
)
async def test_mongodb_refuses_hosts_it_cannot_check(_live: pytest.MonkeyPatch, host: str) -> None:
    fake_ctor = MagicMock()
    _live.setattr(mongo_session, "AsyncMongoClient", fake_ctor)
    with pytest.raises(TargetDestinationBlockedError):
        await mongo_session.connect_client(_target(host), None)
    fake_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_mongodb_refuses_an_allowlisted_name_with_a_path(_live: pytest.MonkeyPatch) -> None:
    """An allowlisted name is dialed by name, so it must be one plain host."""
    host = "a.example.test/admin"
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "a.example.test")
    fake_ctor = MagicMock()
    _live.setattr(mongo_session, "AsyncMongoClient", fake_ctor)
    with pytest.raises(ValueError, match="one host name or IP address"):
        await mongo_session.connect_client(_target(host), None)
    fake_ctor.assert_not_called()


# ---------------------------------------------------------------------------
# Kubernetes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_screening_resolver_answers_with_checked_addresses_only() -> None:
    results = await ScreeningResolver().resolve("public.example.test", 6443, socket.AF_UNSPEC)
    assert [(r["hostname"], r["host"], r["port"]) for r in results] == [
        ("public.example.test", _PUBLIC_IP, 6443),
        ("public.example.test", _PUBLIC_IP_2, 6443),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize("host", ["private.example.test", "gone.example.test"])
async def test_screening_resolver_refuses(host: str) -> None:
    with pytest.raises(TargetDestinationBlockedError):
        await ScreeningResolver().resolve(host, 6443, socket.AF_UNSPEC)


def _api_client(server: str) -> k8s_client.ApiClient:
    configuration = k8s_client.Configuration(host=server)
    return k8s_client.ApiClient(configuration=configuration)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_k8s_client_resolves_through_the_check() -> None:
    api_client = _api_client("https://public.example.test:6443")
    try:
        await check_api_client_dials(api_client)
        connector = api_client.rest_client.pool_manager.connector
        assert isinstance(connector, aiohttp.TCPConnector)
        assert isinstance(connector._resolver, ScreeningResolver)
    finally:
        await api_client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize(
    "server",
    ["https://169.254.169.254:6443", "https://private.example.test", "https://gone.example.test"],
)
async def test_k8s_client_refuses_a_blocked_api_server(server: str) -> None:
    api_client = _api_client(server)
    try:
        with pytest.raises(TargetDestinationBlockedError):
            await check_api_client_dials(api_client)
    finally:
        await api_client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_supervisor_login_refuses_a_blocked_host() -> None:
    with pytest.raises(TargetDestinationBlockedError):
        await wcp_login(
            "169.254.169.254",
            username="u",
            password="p",
            verify_tls=True,
            ca_pem=None,
        )


# ---------------------------------------------------------------------------
# OVF upload client (lease device host)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize("thumbprint", [None, "ab" * 32])
@pytest.mark.parametrize("url", ["https://10.0.0.9/nfc/disk-0", "https://gone.example.test/d"])
async def test_ovf_upload_refuses_a_blocked_device_host(thumbprint: str | None, url: str) -> None:
    target = types.SimpleNamespace(verify_tls=True)
    async with ovf_upload_tls.upload_client(target, thumbprint) as http:
        with pytest.raises(TargetDestinationBlockedError):
            await http.put(url, content=b"x")


@pytest.mark.asyncio
async def test_ovf_upload_dials_only_the_checked_ip(_live: pytest.MonkeyPatch) -> None:
    dialed: list[str] = []

    async def _refuse(self: Any, host: str, port: int, **kwargs: Any) -> Any:
        dialed.append(host)
        raise httpcore.ConnectError("refused in test")

    _live.setattr("httpcore.AnyIOBackend.connect_tcp", _refuse)
    target = types.SimpleNamespace(verify_tls=True)
    async with ovf_upload_tls.upload_client(target, "ab" * 32) as http:
        with pytest.raises(httpx.ConnectError):
            await http.put("https://public.example.test/nfc/disk-0", content=b"x")
    assert dialed
    assert set(dialed) <= {_PUBLIC_IP, _PUBLIC_IP_2}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_k8s_probe_reports_a_blocked_host_as_not_reachable() -> None:
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector

    result = await KubernetesConnector().probe(_target("private.example.test"))
    assert result.ok is False
    assert "TargetDestinationBlockedError" in (result.reason or "")
