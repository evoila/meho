# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Allowlist entries that carry a port (``host:port``).

A bare entry admits a destination on any port. An entry with a port admits
it on that port only. The port is known from a value embedded in the stored
host, or from the caller (the HTTP dial path and the database/SSH paths pass
the target's port). Where no port is known, an entry with a port never
matches, so the guard stays closed.
"""

from __future__ import annotations

import ipaddress
import types
from typing import Any

import httpcore
import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.api.v1.targets import _screen_sent_destination
from meho_backplane.connectors._shared.dial_guard import connect_checked, screen_dial_addresses
from meho_backplane.connectors._shared.pinned_transport import _PortPinnedAsyncBackend
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.adapters.http import (
    HttpConnector,
    SsrfBlockedError,
    _pin_target_addresses,
)
from meho_backplane.connectors.mongodb import session as mongodb_session
from meho_backplane.connectors.mssql import session as mssql_session
from meho_backplane.connectors.postgres import session as postgres_session
from meho_backplane.connectors.targets import update as update_module
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
    assert_public_destination,
    is_allowlisted,
    screen_and_resolve,
)

_HOST = "svc.lab.internal"


@pytest.fixture(autouse=True)
def _guard_live(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Run the guard unexempted, with the name resolving to private space."""
    monkeypatch.delenv(TARGET_SSRF_ALLOWLIST_ENV, raising=False)
    addrs = [ipaddress.ip_address("10.20.30.40")]
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", lambda host: addrs)
    return monkeypatch


def _allow(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, value)


def test_host_port_entry_admits_only_that_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert screen_and_resolve(_HOST, 8443) is None
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(_HOST, 9443)


def test_bare_host_entry_still_admits_any_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, _HOST)
    for port in (None, 22, 443, 8443):
        assert screen_and_resolve(_HOST, port) is None


def test_host_port_entry_never_matches_an_unknown_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(_HOST)


def test_host_port_entry_is_case_insensitive_and_ignores_trailing_dot(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, "Svc.Lab.Internal.:8443")
    assert screen_and_resolve(_HOST, 8443) is None


def test_port_embedded_in_the_stored_host_is_the_dialed_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert screen_and_resolve(f"{_HOST}:8443") is None
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(f"{_HOST}:9443")
    # The embedded port wins over the caller's port.
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(f"{_HOST}:9443", 8443)


def test_other_host_is_not_admitted_by_a_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("other.lab.internal", 8443)


def test_ip_with_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, "192.168.7.10:8443")
    assert_public_destination("192.168.7.10", 8443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("192.168.7.10", 443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("192.168.7.10")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("192.168.7.11", 8443)


def test_ipv6_with_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, "[fd00::5]:8443")
    assert_public_destination("fd00::5", 8443)
    assert_public_destination("[fd00::5]", 8443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("fd00::5", 443)


def test_cidr_range_stays_any_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, "10.0.0.0/8")
    assert_public_destination("10.1.2.3", 22)
    assert_public_destination("10.1.2.3")


@pytest.mark.parametrize(
    "entry", [f"{_HOST}:0", f"{_HOST}:65536", f"{_HOST}:http", "10.0.0.5:99999"]
)
def test_bad_port_in_an_entry_fails_loud(_guard_live: pytest.MonkeyPatch, entry: str) -> None:
    _allow(_guard_live, entry)
    with pytest.raises(ValueError, match="port"):
        assert_public_destination(_HOST, 443)


def test_is_allowlisted_with_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443,192.168.7.10:8443")
    assert is_allowlisted(_HOST, 8443)
    assert is_allowlisted("192.168.7.10", 8443)
    assert not is_allowlisted(_HOST, 443)
    assert not is_allowlisted(_HOST)
    assert not is_allowlisted("192.168.7.10", 443)


def test_target_create_checks_the_embedded_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    created = TargetCreate(name="t1", product="vcenter", host=f"{_HOST}:8443")
    assert created.host == f"{_HOST}:8443"
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host=f"{_HOST}:9443")


def test_create_uses_the_target_port_for_a_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    created = TargetCreate(name="t1", product="vcenter", host=_HOST, port=8443)
    assert created.port == 8443
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host=_HOST, port=9443)
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host=_HOST)
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host="other.lab.internal", port=8443, fqdn=_HOST)


def test_update_with_a_body_port_uses_that_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert TargetUpdate(host=_HOST, port=8443).host == _HOST
    with pytest.raises(ValidationError):
        TargetUpdate(host=_HOST, port=9443)


def test_update_without_a_body_port_uses_the_stored_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    body = TargetUpdate(host=_HOST)  # the schema defers: the stored port is unknown here
    updates = body.model_dump(exclude_unset=True)
    _screen_sent_destination(updates, _stored(port=8443))
    with pytest.raises(HTTPException) as excinfo:
        _screen_sent_destination(updates, _stored(port=9443))
    assert excinfo.value.status_code == 422


def _stored(*, host: str = _HOST, fqdn: str | None = None, port: int | None) -> Any:
    return types.SimpleNamespace(host=host, fqdn=fqdn, port=port)


def test_update_of_the_port_alone_checks_the_stored_host(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    _screen_sent_destination({"port": 8443}, _stored(port=9443))
    with pytest.raises(HTTPException) as excinfo:
        _screen_sent_destination({"port": 9443}, _stored(port=8443))
    assert excinfo.value.status_code == 422
    with pytest.raises(HTTPException):
        _screen_sent_destination({"port": 9443}, _stored(host="203.0.113.9", fqdn=_HOST, port=8443))


def test_update_that_clears_the_port_checks_with_no_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    with pytest.raises(HTTPException):
        _screen_sent_destination({"port": None}, _stored(port=8443))
    with pytest.raises(HTTPException):
        _screen_sent_destination({"host": _HOST, "port": None}, _stored(port=8443))


def test_update_without_destination_fields_checks_nothing(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    _screen_sent_destination({"notes": "x"}, _stored(port=9443))


def test_update_op_checks_a_port_only_change(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    update_module._screen_sent_destination(TargetUpdate(port=8443), _stored(port=9443))
    with pytest.raises(ValueError, match="invalid target parameters"):
        update_module._screen_sent_destination(TargetUpdate(port=9443), _stored(port=8443))
    with pytest.raises(ValueError, match="invalid target parameters"):
        update_module._screen_sent_destination(TargetUpdate(port=None), _stored(port=8443))


# --- an explicit :443 in the stored host -------------------------------------


def test_explicit_https_port_in_the_host_is_kept(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:443")
    assert screen_and_resolve(f"{_HOST}:443") is None
    # The embedded port wins over another caller port.
    assert screen_and_resolve(f"{_HOST}:443", 8443) is None
    assert TargetCreate(name="t1", product="vcenter", host=f"{_HOST}:443").host == f"{_HOST}:443"


def test_explicit_https_port_in_the_host_is_not_another_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(f"{_HOST}:443", 8443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(f"{_HOST}:443")


# --- connect time -----------------------------------------------------------


def test_dial_guard_passes_a_port_entry_through_by_name(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert screen_dial_addresses(_HOST, 8443) is None
    with pytest.raises(TargetDestinationBlockedError):
        screen_dial_addresses(_HOST, 9443)
    with pytest.raises(TargetDestinationBlockedError):
        screen_dial_addresses(_HOST)


class _Probe(HttpConnector):
    product = "probe"

    async def auth_headers(self, target: Any, operator: Any) -> dict[str, str]:
        return {}

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


def _target(host: str, port: int | None) -> Any:
    return types.SimpleNamespace(
        name="ssrf-port-target",
        host=host,
        port=port,
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="00000000-0000-0000-0000-000000000000",
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )


class _PlainHttpProbe(_Probe):
    """A connector whose default scheme is plain ``http`` (port 80)."""

    def _base_url(self, target: Any) -> str:
        port = f":{target.port}" if target.port and target.port != 80 else ""
        return f"http://{target.host}{port}"


def test_dial_port_comes_from_the_connector_base_url() -> None:
    assert _Probe()._dial_port(_target(_HOST, 8443)) == 8443
    assert _Probe()._dial_port(_target(_HOST, None)) == 443
    assert _Probe()._dial_port(_target(f"{_HOST}:8443", None)) == 8443
    assert _PlainHttpProbe()._dial_port(_target(_HOST, None)) == 80
    assert _PlainHttpProbe()._dial_port(_target(_HOST, 443)) == 443


async def test_http_connect_checks_the_connector_default_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:443")
    connector = _PlainHttpProbe()
    try:
        with pytest.raises(SsrfBlockedError):
            await connector._http_client(_target(_HOST, None))
        _allow(_guard_live, f"{_HOST}:80")
        assert isinstance(await connector._http_client(_target(_HOST, None)), httpx.AsyncClient)
    finally:
        await connector.aclose()


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.dialed: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dialed.append((host, port))
        return httpcore.AsyncNetworkStream()


async def test_socket_guard_checks_the_port_the_socket_dials(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    connector = _Probe()
    try:
        client = await connector._http_client(_target(_HOST, 8443))
        backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
        assert isinstance(backend, _PortPinnedAsyncBackend)
        inner = _RecordingBackend()
        backend._inner = inner
        await backend.connect_tcp(_HOST, 8443)
        assert inner.dialed == [(_HOST, 8443)]
        with pytest.raises(SsrfBlockedError):
            await backend.connect_tcp(_HOST, 80)
        assert inner.dialed == [(_HOST, 8443)]
    finally:
        await connector.aclose()


async def test_http_connect_admits_the_listed_port_only(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    connector = _Probe()
    try:
        client = await connector._http_client(_target(_HOST, 8443))
        assert isinstance(client, httpx.AsyncClient)
        with pytest.raises(SsrfBlockedError):
            await connector._http_client(_target(_HOST, 9443))
        with pytest.raises(SsrfBlockedError):
            await connector._http_client(_target(_HOST, None))
    finally:
        await connector.aclose()


def test_pinned_resolver_gets_the_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert _pin_target_addresses(_HOST, port=8443) is None
    with pytest.raises(SsrfBlockedError):
        _pin_target_addresses(_HOST, port=9443)


# --- SSH and database dials check the port they dial ------------------------


class _DialedError(Exception):
    pass


def _record_port(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[int | None]:
    ports: list[int | None] = []

    async def _fake_connect_checked(host: str, connect: Any, **kwargs: Any) -> Any:
        ports.append(kwargs.get("port"))
        raise _DialedError

    monkeypatch.setattr(module, "connect_checked", _fake_connect_checked)
    return ports


def _db_target(port: int | None) -> Any:
    return types.SimpleNamespace(name="db", host=_HOST, port=port, secret_ref=None)


async def test_ssh_without_a_port_checks_port_22(_guard_live: pytest.MonkeyPatch) -> None:
    ports = _record_port(_guard_live, ssh_module)
    with pytest.raises(_DialedError):
        await ssh_module._dial(_db_target(None), {"username": "u", "known_hosts": None})
    with pytest.raises(_DialedError):
        await ssh_module._dial(_db_target(2222), {"username": "u", "known_hosts": None})
    assert ports == [22, 2222]


async def test_databases_without_a_port_check_their_default_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    pg = _record_port(_guard_live, postgres_session)
    mongo = _record_port(_guard_live, mongodb_session)
    mssql = _record_port(_guard_live, mssql_session)
    with pytest.raises(_DialedError):
        await postgres_session.connect_read_only(_db_target(None), None)
    with pytest.raises(_DialedError):
        await mongodb_session.connect_client(_db_target(None), None)
    with pytest.raises(_DialedError):
        await mssql_session._connect(_db_target(None), {}, database=None, query_timeout=5)
    assert pg == [postgres_session.DEFAULT_PORT]
    assert mongo == [mongodb_session.DEFAULT_PORT]
    assert mssql == [mssql_session.DEFAULT_PORT]


async def test_connect_checked_checks_the_driver_port_not_the_embedded_one(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, "192.168.7.10:443")
    dialed: list[str] = []

    async def _connect(address: str) -> str:
        dialed.append(address)
        return address

    with pytest.raises(TargetDestinationBlockedError):
        await connect_checked("192.168.7.10:443", _connect, port=22)
    assert dialed == []
    assert await connect_checked("192.168.7.10:443", _connect, port=443) == "192.168.7.10"


def _record_dial(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> list[Any]:
    calls: list[Any] = []

    def _fake(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise _DialedError

    monkeypatch.setattr(owner, name, _fake)
    return calls


@pytest.mark.parametrize("port", [None, 2222])
async def test_ssh_checks_its_own_port_not_a_port_in_the_host(
    _guard_live: pytest.MonkeyPatch, port: int | None
) -> None:
    _allow(_guard_live, "192.168.7.10:443")
    calls = _record_dial(_guard_live, ssh_module.asyncssh, "connect")
    target = types.SimpleNamespace(name="db", host="192.168.7.10:443", port=port, secret_ref=None)
    with pytest.raises(TargetDestinationBlockedError):
        await ssh_module._dial(target, {"username": "u", "known_hosts": None})
    assert calls == []


@pytest.mark.parametrize("port", [None, 15432])
async def test_databases_check_their_own_port_not_a_port_in_the_host(
    _guard_live: pytest.MonkeyPatch, port: int | None
) -> None:
    _allow(_guard_live, "192.168.7.10:443")
    pg = _record_dial(_guard_live, postgres_session.asyncpg, "connect")
    mongo = _record_dial(_guard_live, mongodb_session, "AsyncMongoClient")
    mssql = _record_dial(_guard_live, mssql_session, "_blocking_connect")
    target = types.SimpleNamespace(name="db", host="192.168.7.10:443", port=port, secret_ref=None)
    with pytest.raises(TargetDestinationBlockedError):
        await postgres_session.connect_read_only(target, None)
    with pytest.raises(TargetDestinationBlockedError):
        await mongodb_session.connect_client(target, None)
    with pytest.raises(TargetDestinationBlockedError):
        await mssql_session._connect(target, {}, database=None, query_timeout=5)
    assert pg == mongo == mssql == []


async def test_driver_port_that_matches_the_entry_is_admitted(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, "192.168.7.10:2222")
    calls = _record_dial(_guard_live, ssh_module.asyncssh, "connect")
    target = types.SimpleNamespace(name="db", host="192.168.7.10:443", port=2222, secret_ref=None)
    with pytest.raises(_DialedError):
        await ssh_module._dial(target, {"username": "u", "known_hosts": None})
    assert calls[0][0][0] == "192.168.7.10"
    assert calls[0][1]["port"] == 2222
