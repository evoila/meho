# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared connect-time address check (``dial_guard``).

Every driver checks the target address again when it connects, and dials
only checked IP addresses, the next one when a connection attempt fails:

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

import asyncio
import contextlib
import ipaddress
import types
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpcore
import httpx
import pytest
from aiohttp import web
from kubernetes_asyncio import client as k8s_client
from pymongo.errors import ServerSelectionTimeoutError

from meho_backplane.connectors._shared import dial_guard
from meho_backplane.connectors._shared.dial_guard import (
    DialHostUnresolvedError,
    connect_checked,
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
from meho_backplane.connectors.secret import handoff as handoff_module
from meho_backplane.connectors.vmware_rest import ovf_upload_tls
from meho_backplane.connectors.vmware_rest._transfer import (
    Destination,
    SourceFile,
    TransferError,
    copy_files,
)
from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
)

# Captured at import, before the suite-wide fixture replaces it.
_REAL_UNRESOLVED = dial_guard._unresolved

_PUBLIC_IP = "93.184.216.34"
_PUBLIC_IP_2 = "93.184.216.35"
_PUBLIC_IP6 = "2606:2800:220:1:248:1893:25c8:1946"

#: Fake DNS: name -> answers. A name missing here does not resolve.
_DNS: dict[str, list[str]] = {
    "public.example.test": [_PUBLIC_IP, _PUBLIC_IP_2],
    "private.example.test": ["10.0.0.7"],
    "metadata.example.test": ["169.254.169.254"],
    "mixed.example.test": [_PUBLIC_IP, "127.0.0.1"],
    "v6.example.test": [_PUBLIC_IP6],
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


def _count_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every DNS lookup the check makes."""
    lookups: list[str] = []

    def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        lookups.append(host)
        return _fake_resolve(host)

    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", _resolve)
    return lookups


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_connect_checked_dials_the_first_checked_address() -> None:
    connect = AsyncMock(return_value="conn")
    assert await connect_checked("public.example.test", connect) == "conn"
    connect.assert_awaited_once_with(_PUBLIC_IP)


@pytest.mark.asyncio
async def test_connect_checked_tries_the_next_address_without_a_second_lookup(
    _live: pytest.MonkeyPatch,
) -> None:
    lookups = _count_lookups(_live)
    connect = AsyncMock(side_effect=[ConnectionRefusedError("refused"), "conn"])
    assert await connect_checked("public.example.test", connect) == "conn"
    assert [call.args[0] for call in connect.await_args_list] == [_PUBLIC_IP, _PUBLIC_IP_2]
    assert lookups == ["public.example.test"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_connect_checked_raises_the_last_error_when_every_address_fails() -> None:
    connect = AsyncMock(side_effect=[ConnectionRefusedError("one"), TimeoutError("two")])
    with pytest.raises(TimeoutError, match="two"):
        await connect_checked("public.example.test", connect)
    assert connect.await_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_connect_checked_does_not_retry_other_errors() -> None:
    connect = AsyncMock(side_effect=PermissionError("login failed"))
    with pytest.raises(PermissionError):
        await connect_checked("public.example.test", connect, retry_on=(ConnectionRefusedError,))
    connect.assert_awaited_once_with(_PUBLIC_IP)


@pytest.mark.asyncio
async def test_connect_checked_hands_an_allowlisted_name_over_as_written(
    _live: pytest.MonkeyPatch,
) -> None:
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "gone.example.test")
    connect = AsyncMock(return_value="conn")
    assert await connect_checked("gone.example.test", connect) == "conn"
    connect.assert_awaited_once_with("gone.example.test")


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


@pytest.mark.asyncio
async def test_ssh_tries_the_next_checked_ip_when_the_first_refuses(
    _live: pytest.MonkeyPatch,
) -> None:
    connector, fake_connect = _ssh_with_fake_connect(_live)
    conn = fake_connect.return_value
    fake_connect.side_effect = [ConnectionRefusedError("refused"), conn]
    assert await connector._connect(_target("public.example.test")) is conn
    assert [call.args[0] for call in fake_connect.await_args_list] == [_PUBLIC_IP, _PUBLIC_IP_2]
    assert fake_connect.await_args_list[1].kwargs["host_key_alias"] == "public.example.test"


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
async def test_postgres_tries_the_next_checked_ip_when_the_first_refuses(
    _live: pytest.MonkeyPatch,
) -> None:
    """One lookup, two checked answers: the second is dialed when the first refuses."""
    lookups = _count_lookups(_live)
    conn = MagicMock()
    fake_connect = AsyncMock(side_effect=[ConnectionRefusedError("refused"), conn])
    _live.setattr(pg_session.asyncpg, "connect", fake_connect)
    assert await pg_session.connect_read_only(_target("public.example.test"), None) is conn
    assert [call.kwargs["host"] for call in fake_connect.await_args_list] == [
        _PUBLIC_IP,
        _PUBLIC_IP_2,
    ]
    assert lookups == ["public.example.test"]


def _mssql_with_fake_connect(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_connect = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(mssql_session, "_blocking_connect", fake_connect)
    monkeypatch.setattr(mssql_session, "_blocking_fetch", MagicMock(return_value=[]))
    monkeypatch.setattr(mssql_session, "_blocking_execute", MagicMock())
    monkeypatch.setattr(
        mssql_session,
        "resolve_sql_credentials",
        AsyncMock(return_value={"sql_username": "u", "sql_password": "p"}),
    )
    return fake_connect


@pytest.mark.asyncio
async def test_mssql_dials_the_checked_ip(_live: pytest.MonkeyPatch) -> None:
    fake_connect = _mssql_with_fake_connect(_live)
    await mssql_session.fetch_rows(_target("public.example.test"), None, "SELECT 1")
    assert fake_connect.call_args.kwargs["host"] == _PUBLIC_IP


@pytest.mark.asyncio
async def test_mssql_refuses_before_connecting(_live: pytest.MonkeyPatch) -> None:
    fake_connect = _mssql_with_fake_connect(_live)
    with pytest.raises(TargetDestinationBlockedError):
        await mssql_session.execute_statement(_target("private.example.test"), None, "SELECT 1")
    fake_connect.assert_not_called()
    mssql_session._blocking_execute.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mssql_tries_the_next_checked_ip_when_the_first_refuses(
    _live: pytest.MonkeyPatch,
) -> None:
    fake_connect = _mssql_with_fake_connect(_live)
    conn = fake_connect.return_value
    fake_connect.side_effect = [ConnectionRefusedError("refused"), conn]
    await mssql_session.execute_statement(_target("public.example.test"), None, "SELECT 1")
    assert [call.kwargs["host"] for call in fake_connect.call_args_list] == [
        _PUBLIC_IP,
        _PUBLIC_IP_2,
    ]
    mssql_session._blocking_execute.assert_called_once()  # type: ignore[attr-defined]
    assert mssql_session._blocking_execute.call_args.args[0] is conn  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# MongoDB: the client reaches only the checked host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mongodb_client_knows_only_the_checked_address(_live: pytest.MonkeyPatch) -> None:
    """The real client is built (the ping is stubbed) and its topology holds one checked seed.

    A direct connection to one IP address means the driver makes no SRV
    lookup and adds no replica-set members it learns from the server.
    """
    _live.setattr(mongo_session, "_ping", AsyncMock())
    mongo = await mongo_session.connect_client(_target("public.example.test"), None)
    try:
        description = mongo.topology_description
        assert description.topology_type_name == "Single"
        assert list(description.server_descriptions()) == [(_PUBLIC_IP, mongo_session.DEFAULT_PORT)]
    finally:
        await mongo.close()


@pytest.mark.asyncio
async def test_mongodb_gives_a_checked_ipv6_address_in_brackets(
    _live: pytest.MonkeyPatch,
) -> None:
    """pymongo does not take a bare IPv6 address; the client still holds one checked seed."""
    _live.setattr(mongo_session, "_ping", AsyncMock())
    mongo = await mongo_session.connect_client(_target("v6.example.test"), None)
    try:
        description = mongo.topology_description
        assert description.topology_type_name == "Single"
        assert list(description.server_descriptions()) == [
            (_PUBLIC_IP6, mongo_session.DEFAULT_PORT)
        ]
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
async def test_mongodb_tries_the_next_checked_ip_when_the_first_does_not_answer(
    _live: pytest.MonkeyPatch,
) -> None:
    """The lazy client is pinged; a dead first address is closed and the next one is tried."""
    dead = MagicMock()
    dead.get_database.return_value.command = AsyncMock(
        side_effect=ServerSelectionTimeoutError("no server")
    )
    dead.close = AsyncMock()
    alive = MagicMock()
    alive.get_database.return_value.command = AsyncMock(return_value={"ok": 1.0})
    fake_ctor = MagicMock(side_effect=[dead, alive])
    _live.setattr(mongo_session, "AsyncMongoClient", fake_ctor)
    assert await mongo_session.connect_client(_target("public.example.test"), None) is alive
    assert [call.kwargs["host"] for call in fake_ctor.call_args_list] == [_PUBLIC_IP, _PUBLIC_IP_2]
    dead.close.assert_awaited_once()


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


def _api_client(server: str) -> k8s_client.ApiClient:
    configuration = k8s_client.Configuration(host=server)
    return k8s_client.ApiClient(configuration=configuration)


def _connector_of(api_client: k8s_client.ApiClient) -> aiohttp.TCPConnector:
    connector = api_client.rest_client.pool_manager.connector
    assert isinstance(connector, aiohttp.TCPConnector)
    return connector


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
async def test_k8s_client_resolves_a_name_through_the_check() -> None:
    api_client = _api_client("https://public.example.test:6443")
    try:
        await check_api_client_dials(api_client)
        results = await _connector_of(api_client)._resolve_host("public.example.test", 6443)
        assert [(r["hostname"], r["host"], r["port"]) for r in results] == [
            ("public.example.test", _PUBLIC_IP, 6443),
            ("public.example.test", _PUBLIC_IP_2, 6443),
        ]
    finally:
        await api_client.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize(
    "host", ["10.0.0.9", "169.254.169.254", "private.example.test", "gone.example.test"]
)
async def test_k8s_client_checks_every_host_it_dials(host: str) -> None:
    """A later dial to a blocked literal or name is refused, also when the API server passed."""
    api_client = _api_client("https://public.example.test:6443")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(TargetDestinationBlockedError):
            await _connector_of(api_client)._resolve_host(host, 443)
    finally:
        await api_client.close()


@pytest.mark.asyncio
async def test_k8s_client_refuses_a_redirect_to_a_blocked_address(
    _live: pytest.MonkeyPatch,
) -> None:
    """A local server redirects to a blocked IP literal; the client does not follow it."""
    hits: list[str] = []

    async def _redirect(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("http://10.0.0.9/redirected")

    app = web.Application()
    app.router.add_get("/api", _redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.1/32")
    api_client = _api_client(f"http://127.0.0.1:{port}")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(TargetDestinationBlockedError):
            await api_client.rest_client.pool_manager.get(f"http://127.0.0.1:{port}/api")
        assert hits == ["/api"]
    finally:
        await api_client.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_live")
@pytest.mark.parametrize("proxy", ["http://10.0.0.9:3128", "http://private.example.test:3128"])
async def test_k8s_client_refuses_a_blocked_proxy(proxy: str) -> None:
    api_client = _api_client("https://public.example.test:6443")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(TargetDestinationBlockedError):
            await api_client.rest_client.pool_manager.get(
                "https://public.example.test:6443/api", proxy=proxy
            )
    finally:
        await api_client.close()


class _TlsStoppedError(Exception):
    """Raised by the test's TLS stand-in once it has seen the TLS name."""


async def _fake_proxy(answers: list[bytes]) -> tuple[asyncio.Server, int, list[bytes]]:
    """A local proxy stand-in: records each request head and answers them in order."""
    heads: list[bytes] = []

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(answers[min(len(heads), len(answers)) - 1])
        await writer.drain()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.read(), 5)
        writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], heads


def _stop_at_tls(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Replace the loop's TLS start: record the TLS name, then stop."""
    tls_names: list[str | None] = []

    async def _start_tls(*_args: Any, server_hostname: str | None = None, **_kw: Any) -> None:
        tls_names.append(server_hostname)
        raise _TlsStoppedError

    monkeypatch.setattr(asyncio.get_running_loop(), "start_tls", _start_tls)
    return tls_names


async def _get_through_proxy(api_client: k8s_client.ApiClient, url: str, port: int) -> None:
    await api_client.rest_client.pool_manager.get(url, proxy=f"http://127.0.0.1:{port}")


@pytest.mark.asyncio
async def test_k8s_client_asks_the_proxy_for_a_checked_address(_live: pytest.MonkeyPatch) -> None:
    """The tunnel request names the checked address; Host and the TLS name keep the name."""
    proxy, port, heads = await _fake_proxy([b"HTTP/1.1 200 Connection established\r\n\r\n"])
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.1/32")
    tls_names = _stop_at_tls(_live)
    api_client = _api_client("https://public.example.test:6443")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(_TlsStoppedError):
            await _get_through_proxy(api_client, "https://public.example.test:6443/api", port)
    finally:
        await api_client.close()
        proxy.close()
        await proxy.wait_closed()
    assert len(heads) == 1
    assert heads[0].startswith(f"CONNECT {_PUBLIC_IP}:6443 HTTP/1.1\r\n".encode())
    assert b"Host: public.example.test:6443\r\n" in heads[0]
    assert tls_names == ["public.example.test"]


@pytest.mark.asyncio
async def test_k8s_client_asks_the_proxy_for_the_next_checked_address(
    _live: pytest.MonkeyPatch,
) -> None:
    """When the proxy could not connect, the next checked address is asked for."""
    proxy, port, heads = await _fake_proxy(
        [
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 Connection established\r\n\r\n",
        ]
    )
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.1/32")
    tls_names = _stop_at_tls(_live)
    api_client = _api_client("https://public.example.test:6443")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(_TlsStoppedError):
            await _get_through_proxy(api_client, "https://public.example.test:6443/api", port)
    finally:
        await api_client.close()
        proxy.close()
        await proxy.wait_closed()
    assert [h.split(b"\r\n", 1)[0] for h in heads] == [
        f"CONNECT {_PUBLIC_IP}:6443 HTTP/1.1".encode(),
        f"CONNECT {_PUBLIC_IP_2}:6443 HTTP/1.1".encode(),
    ]
    assert tls_names == ["public.example.test"]


@pytest.mark.asyncio
async def test_k8s_client_refuses_a_redirect_through_a_proxy(_live: pytest.MonkeyPatch) -> None:
    """A plain request names the checked address; a redirect to a blocked one is not followed."""
    proxy, port, heads = await _fake_proxy(
        [b"HTTP/1.1 302 Found\r\nLocation: http://10.0.0.9/redirected\r\nContent-Length: 0\r\n\r\n"]
    )
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.1/32")
    api_client = _api_client("http://public.example.test")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(TargetDestinationBlockedError):
            await _get_through_proxy(api_client, "http://public.example.test/api", port)
    finally:
        await api_client.close()
        proxy.close()
        await proxy.wait_closed()
    assert len(heads) == 1
    assert heads[0].startswith(f"GET http://{_PUBLIC_IP}/api HTTP/1.1\r\n".encode())
    assert b"Host: public.example.test\r\n" in heads[0]


@pytest.mark.asyncio
async def test_k8s_client_hands_an_allowlisted_name_to_the_proxy(_live: pytest.MonkeyPatch) -> None:
    proxy, port, heads = await _fake_proxy([b"HTTP/1.1 200 Connection established\r\n\r\n"])
    _live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "127.0.0.1/32,gone.example.test")
    tls_names = _stop_at_tls(_live)
    api_client = _api_client("https://gone.example.test")
    try:
        await check_api_client_dials(api_client)
        with pytest.raises(_TlsStoppedError):
            await _get_through_proxy(api_client, "https://gone.example.test/api", port)
    finally:
        await api_client.close()
        proxy.close()
        await proxy.wait_closed()
    assert heads[0].startswith(b"CONNECT gone.example.test:443 HTTP/1.1\r\n")
    assert tls_names == ["gone.example.test"]


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


# ---------------------------------------------------------------------------
# Secret handoff store client and vCenter file copy destination
# ---------------------------------------------------------------------------


def _record_dials(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every TCP connect the HTTP clients try, and refuse it."""
    dialed: list[str] = []

    async def _refuse(self: Any, host: str, port: int, **kwargs: Any) -> Any:
        dialed.append(host)
        raise httpcore.ConnectError("refused in test")

    monkeypatch.setattr("httpcore.AnyIOBackend.connect_tcp", _refuse)
    return dialed


def _store(monkeypatch: pytest.MonkeyPatch, host: str) -> Any:
    monkeypatch.setattr(
        handoff_module, "get_settings", lambda: types.SimpleNamespace(vault_timeout_seconds=5.0)
    )
    return handoff_module._RemoteStore(
        label="store target 'vault-a'", host=host, url=f"https://{host}", verify=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["gone.example.test", "private.example.test"])
async def test_handoff_store_client_refuses_before_connecting(
    _live: pytest.MonkeyPatch, host: str
) -> None:
    dialed = _record_dials(_live)
    async with handoff_module._build_store_client(_store(_live, host)) as client:
        with pytest.raises(handoff_module.SecretHandoffDestinationBlockedError):
            await client.post("/v1/sys/wrapping/unwrap", json={})
    assert dialed == []


@pytest.mark.asyncio
async def test_handoff_store_client_dials_only_the_checked_ip(_live: pytest.MonkeyPatch) -> None:
    dialed = _record_dials(_live)
    async with handoff_module._build_store_client(_store(_live, "public.example.test")) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("/v1/sys/wrapping/unwrap", json={})
    assert dialed
    assert set(dialed) <= {_PUBLIC_IP, _PUBLIC_IP_2}


async def _copy_to(base_url: str) -> None:
    async def _noop() -> None:
        return None

    async with httpx.AsyncClient() as source_client:
        await copy_files(
            [SourceFile(name="disk-1.vmdk", url="https://vcenter.example.test/d", size=1)],
            Destination(base_url=base_url, token_ref="tenants/t1/backup-store"),
            operator=object(),  # type: ignore[arg-type]
            target=types.SimpleNamespace(name="vc-1", host="vcenter.example.test"),
            source_client=source_client,
            source_headers={},
            keep_alive=_noop,
            cleanup=_noop,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["gone.example.test", "private.example.test"])
async def test_file_copy_destination_refuses_before_connecting(
    _live: pytest.MonkeyPatch, host: str
) -> None:
    dialed = _record_dials(_live)
    _live.setattr(
        "meho_backplane.connectors.vmware_rest._transfer._read_token",
        AsyncMock(return_value="dest-token"),
    )
    with pytest.raises(TransferError):
        await _copy_to(f"https://{host}/backups/")
    assert dialed == []


@pytest.mark.asyncio
async def test_file_copy_destination_dials_only_the_checked_ip(_live: pytest.MonkeyPatch) -> None:
    dialed = _record_dials(_live)
    _live.setattr(
        "meho_backplane.connectors.vmware_rest._transfer._read_token",
        AsyncMock(return_value="dest-token"),
    )
    with pytest.raises(TransferError):
        await _copy_to("https://public.example.test/backups/")
    assert dialed
    assert set(dialed) <= {_PUBLIC_IP, _PUBLIC_IP_2}
