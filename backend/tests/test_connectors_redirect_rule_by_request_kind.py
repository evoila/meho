# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each kind of request: its redirect rule, and host names only in MEHO's text.

The kinds are a read, a change, the login of the shared session
(``vcf_session_login``), a profiled login, and a connector's own login (the
Proxmox ticket login of ``proxmox.about``). Each runs with the switch
``REDIRECT_ON_CHANGE_RESULT`` off and on.

- A read and the three logins follow a redirect on the same scheme, host and
  port, also to another path. A change follows only a 307 or 308 to the same
  path with the same query, and is sent at most twice.
- Each call ends on a redirect to another host. The ``Location`` holds
  made-up values in its path, query, fragment, user part and port.
- MEHO's own text names each address only by its host name: ``error`` and
  ``extras.exception_message`` of the answer and of the audit row, and the
  log events of a refused redirect.

The read, the change and the Proxmox login run through the real
``dispatch`` against a local HTTP server. The two other logins run at the
client level. Names and addresses are made up.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.login_redirect import SessionLoginRedirectError
from meho_backplane.connectors._shared.vcf_auth import SessionLoginError, vcf_session_login
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.adapters.http import _SameOriginRedirectClient
from meho_backplane.connectors.proxmox import ProxmoxConnector
from meho_backplane.connectors.proxmox.session import ProxmoxCredentials
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target
from tests.test_connectors_profiled_auth import _connector as _profiled_connector
from tests.test_connectors_profiled_auth import _StubTarget

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000d304")
_PRODUCT = "kinddemo"
_VERSION = "1.x"
_IMPL_ID = "kinddemo-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_READ_OP = "kinddemo.thing.get"
_CHANGE_OP = "kinddemo.thing.update"
_START = "/api/thing"
_OTHER_HOST = "other.test.invalid"

_REFUSAL_EVENTS = (
    "connector_redirect_not_followed_cross_origin",
    "connector_redirect_not_followed_on_change",
)


def _made_up() -> str:
    """A made-up value no other hide rule knows: "zq" and 28 random hex characters."""
    return "zq" + secrets.token_hex(14)


@dataclass
class _Away:
    """A ``Location`` on another host, with a made-up value in each part."""

    user: str = field(default_factory=_made_up)
    password: str = field(default_factory=_made_up)
    path: str = field(default_factory=_made_up)
    query: str = field(default_factory=_made_up)
    fragment: str = field(default_factory=_made_up)
    port: str = field(default_factory=lambda: str(10000 + secrets.randbelow(55000)))

    @property
    def location(self) -> str:
        return (
            f"http://{self.user}:{self.password}@{_OTHER_HOST}:{self.port}"
            f"/{self.path}?q={self.query}#{self.fragment}"
        )

    @property
    def values(self) -> list[str]:
        return [self.user, self.password, self.path, self.query, self.fragment, self.port]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def answer_switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Set the switch ``REDIRECT_ON_CHANGE_RESULT`` for one test."""

    def set_to(on: bool) -> None:
        monkeypatch.setenv("REDIRECT_ON_CHANGE_RESULT", "true" if on else "false")
        get_settings.cache_clear()

    yield set_to
    get_settings.cache_clear()


def _assert_host_names_only(texts: dict[str, str], values: list[str]) -> None:
    for place, text in texts.items():
        for value in values:
            assert value not in text, f"a made-up value is in the {place}: {text!r}"


def _refusals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] in _REFUSAL_EVENTS]


# ---------------------------------------------------------------------------
# A local HTTP server
# ---------------------------------------------------------------------------


@dataclass
class _Server:
    server: asyncio.AbstractServer
    port: int
    hits: list[tuple[str, str]] = field(default_factory=list)


async def _start_server(
    routes: dict[str, tuple[str, dict[str, str]]],
    default: tuple[str, dict[str, str]] | None = None,
) -> _Server:
    """A local HTTP/1.1 server: each path (with query) answers its status line and headers.

    A path not in *routes* gets *default*, or a 404.
    """
    state = _Server(server=None, port=0)  # type: ignore[arg-type]

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = (await reader.readline()).decode("latin-1").split()
            method, path = (parts[0], parts[1]) if len(parts) > 1 else ("?", "/")
            length = 0
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            await reader.readexactly(length)
            state.hits.append((method, path))
            line_text, headers = routes.get(path) or default or ("404 Not Found", {})
            head = f"HTTP/1.1 {line_text}\r\nContent-Length: 0\r\nConnection: close\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            writer.write((head + "\r\n").encode("latin-1"))
            await writer.drain()
        finally:
            writer.close()

    state.server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    state.port = state.server.sockets[0].getsockname()[1]
    return state


async def _stop(server: _Server) -> None:
    server.server.close()
    await server.server.wait_closed()


# ---------------------------------------------------------------------------
# A read and a change through the real dispatch
# ---------------------------------------------------------------------------


class _KindConnector(HttpConnector):
    """A typed connector with one read and one change through the shared HTTP seam."""

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError

    async def thing_get(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Read one thing."""
        return await self._get_json(target, _START, operator=operator)

    async def thing_update(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Change one thing."""
        return await self._post_json(
            target, _START, operator=operator, verb="PUT", json={"name": "w"}
        )


@dataclass
class _Target:
    """A plain-HTTP target on the local server."""

    port: int
    product: str = _PRODUCT
    version: str = _VERSION
    host: str = "127.0.0.1"
    name: str = "kind-demo"
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = _TENANT_ID
    secret_ref: str | None = None
    auth_model: str = "shared_service_account"
    tls_server_name: str | None = None
    preferred_impl_id: str | None = None
    extras: dict[str, Any] = field(default_factory=lambda: {"scheme": "http"})

    @property
    def fingerprint(self) -> Any:
        return SimpleNamespace(version=self.version)


@pytest.fixture
def _kind_connector(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_KindConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


async def _register_ops() -> None:
    for op_id, handler, safety in (
        (_READ_OP, _KindConnector.thing_get, "safe"),
        (_CHANGE_OP, _KindConnector.thing_update, "caution"),
    ):
        await register_typed_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
            handler=handler,
            summary="One thing.",
            description="Synthetic operation on one thing.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            tags=["read"] if safety == "safe" else ["write"],
            safety_level=safety,
            requires_approval=False,
        )


def _operator() -> Operator:
    return Operator(
        sub="op-kind-demo",
        name="Kind Demo Operator",
        email=None,
        raw_jwt="op.kind.demo.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        web_session=True,
    )


async def _audit_error(audit_id: UUID) -> tuple[dict[str, Any], str]:
    async with get_sessionmaker()() as s:
        audit = (await s.execute(select(AuditLog).where(AuditLog.id == audit_id))).scalar_one()
    return audit.payload["error"], json.dumps(
        {column.name: getattr(audit, column.key) for column in audit.__mapper__.columns},
        default=str,
    )


async def _dispatch(
    op_id: str, routes: dict[str, tuple[str, dict[str, str]]], *, approved: bool = False
) -> tuple[Any, _Server, list[dict[str, Any]]]:
    await _register_ops()
    server = await _start_server(routes)
    try:
        with capture_logs() as events:
            result = await dispatch(
                operator=_operator(),
                connector_id=_CONNECTOR_ID,
                op_id=op_id,
                target=_Target(port=server.port),
                params={},
                _approved=approved,
            )
    finally:
        await _stop(server)
    return result, server, events


async def _check_answer_and_audit_row(
    result: Any, events: list[dict[str, Any]], away: _Away, extra_values: list[str]
) -> None:
    """MEHO's text in the answer, the audit row and the log events names host names only."""
    assert result.status == "error"
    assert result.audit_id is not None
    audited, audit_text = await _audit_error(result.audit_id)
    message = result.extras["exception_message"]
    assert audited["exception_message"] == message
    values = [*away.values, *extra_values]
    _assert_host_names_only(
        {
            "error": result.error or "",
            "exception_message": message,
            "answer": result.model_dump_json(),
            "audit row": audit_text,
            "log events": repr(_refusals(events)),
        },
        values,
    )
    for event in _refusals(events):
        assert event["from_url"] == "127.0.0.1"
        assert event["location"] in ("127.0.0.1", _OTHER_HOST)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kind_connector")
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_read_follows_the_same_origin_and_ends_on_another_host(
    session: AsyncSession, on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    hop_path, hop_query = _made_up(), _made_up()
    hop = f"/{hop_path}?q={hop_query}"
    away = _Away()
    routes = {
        _START: ("302 Found", {"Location": hop}),
        hop: ("302 Found", {"Location": away.location}),
    }

    result, server, events = await _dispatch(_READ_OP, routes)

    # The read followed the redirect to another path on the same origin.
    assert server.hits == [("GET", _START), ("GET", hop)]
    assert result.error == "connector_error: HTTPStatusError"
    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    [event] = _refusals(events)
    assert event["event"] == "connector_redirect_not_followed_cross_origin"
    assert event["reason"] == "other host"
    await _check_answer_and_audit_row(result, events, away, [hop_path, hop_query])


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kind_connector")
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_change_is_sent_at_most_twice_and_ends_on_another_host(
    session: AsyncSession, on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    away = _Away()
    routes = {
        _START: ("307 Temporary Redirect", {"Location": f"{_START}/"}),
        f"{_START}/": ("302 Found", {"Location": away.location}),
    }

    result, server, events = await _dispatch(_CHANGE_OP, routes, approved=True)

    # A 307 to the same path sends the change once more; the next redirect ends it.
    assert server.hits == [("PUT", _START), ("PUT", f"{_START}/")]
    message = result.extras["exception_message"]
    if on:
        assert message == "HTTP 302 redirect after PUT 127.0.0.1"
        assert f"redirect to {_OTHER_HOST}." in (result.error or "")
    else:
        assert result.error == "connector_error: HTTPStatusError"
        assert message == f"Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    [event] = _refusals(events)
    assert event["event"] == "connector_redirect_not_followed_cross_origin"
    assert event["reason"] == "other host"
    await _check_answer_and_audit_row(result, events, away, [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kind_connector")
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_change_follows_no_redirect_to_another_path(
    session: AsyncSession, on: bool, answer_switch: Any
) -> None:
    # Where a read follows, a change stops: a 303 to another path on the same host.
    answer_switch(on)
    path, query, fragment = _made_up(), _made_up(), _made_up()
    away = _Away()
    routes = {_START: ("303 See Other", {"Location": f"/{path}?q={query}#{fragment}"})}

    result, server, events = await _dispatch(_CHANGE_OP, routes, approved=True)

    assert server.hits == [("PUT", _START)]
    message = result.extras["exception_message"]
    if on:
        assert message == "HTTP 303 redirect after PUT 127.0.0.1"
        assert "another resource at 127.0.0.1." in (result.error or "")
    else:
        assert message == "Redirect response 303 from 127.0.0.1 to <unparseable-location>, " + (
            "not followed."
        )
    [event] = _refusals(events)
    assert event["event"] == "connector_redirect_not_followed_on_change"
    assert (event["from_url"], event["location"]) == ("127.0.0.1", "127.0.0.1")
    await _check_answer_and_audit_row(result, events, away, [path, query, fragment])


# ---------------------------------------------------------------------------
# The Proxmox ticket login through the real dispatch
# ---------------------------------------------------------------------------

_PROXMOX_TENANT = UUID("00000000-0000-0000-0000-00000000d305")
_PROXMOX_TARGET = "proxmox-kind-demo"


def _ticket_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> ProxmoxCredentials:
        return ProxmoxCredentials(mode="ticket", username="demo@pve", password="not-a-password")

    return _load()


@pytest.fixture
async def proxmox() -> AsyncIterator[ProxmoxConnector]:
    reset_dispatcher_caches()
    await ProxmoxConnector.register_operations()
    instance = ProxmoxConnector(credentials_loader=_ticket_loader)
    _CONNECTOR_INSTANCE_CACHE[ProxmoxConnector] = instance
    yield instance
    await instance.aclose()
    _CONNECTOR_INSTANCE_CACHE.pop(ProxmoxConnector, None)
    reset_dispatcher_caches()


async def _seed_proxmox(port: int) -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                tenant_id=_PROXMOX_TENANT,
                name=_PROXMOX_TARGET,
                aliases=[],
                product="proxmox",
                host="127.0.0.1",
                port=port,
                fqdn=None,
                secret_ref="site/proxmox/ticket",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"scheme": "http"},
                fingerprint={"version": "8.x"},
                preferred_impl_id="proxmox-api",
            )
        )
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_the_proxmox_ticket_login_follows_the_same_origin_and_ends_on_another_host(
    proxmox: ProxmoxConnector, on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    hop_path, hop_query = _made_up(), _made_up()
    hop = f"/{hop_path}?q={hop_query}"
    away = _Away()
    # The login (a POST) gets a 307 to another path on the same origin, and
    # follows it as a login does. Then a 302 to another host ends it.
    server = await _start_server(
        {hop: ("302 Found", {"Location": away.location})},
        default=("307 Temporary Redirect", {"Location": hop}),
    )
    try:
        await _seed_proxmox(server.port)
        async with get_sessionmaker()() as s:
            target = await resolve_target(s, _PROXMOX_TENANT, _PROXMOX_TARGET)
        operator = Operator(
            sub="proxmox-kind-demo",
            name="Proxmox Kind Demo Operator",
            email=None,
            raw_jwt="<proxmox-kind-demo-jwt>",
            tenant_id=_PROXMOX_TENANT,
            tenant_role=TenantRole.OPERATOR,
        )
        with capture_logs() as events:
            result = await dispatch(
                operator=operator,
                connector_id="proxmox-api-8.x",
                op_id="proxmox.about",
                target=target,
                params={},
            )
    finally:
        await _stop(server)

    assert [method for method, _ in server.hits] == ["POST", "POST"]
    assert server.hits[1][1] == hop
    message = result.extras["exception_message"]
    if on:
        assert message == (
            "The login of this connector got HTTP 302, a redirect. "
            "MEHO did not follow it, so the login did not finish."
        )
    else:
        assert message == (
            f"HTTPStatusError: Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
        )
    [event] = _refusals(events)
    assert event["event"] == "connector_redirect_not_followed_cross_origin"
    assert event["reason"] == "other host"
    await _check_answer_and_audit_row(result, events, away, [hop_path, hop_query])


# ---------------------------------------------------------------------------
# The shared session login and a profiled login, at the client level
# ---------------------------------------------------------------------------

_LOGIN_BASE = "https://login.example.test"
_LOGIN_PATH = "/api/v2/sessions"


def _login_routes(mock: respx.MockRouter, hop: str, away: _Away) -> tuple[Any, Any, Any]:
    login = mock.post(f"{_LOGIN_BASE}{_LOGIN_PATH}").respond(307, headers={"Location": hop})
    again = mock.post(f"{_LOGIN_BASE}{hop.split('?')[0]}").respond(
        302, headers={"Location": away.location}
    )
    elsewhere = mock.route(host=_OTHER_HOST).respond(200, json={})
    return login, again, elsewhere


def _check_login_refusal(
    events: list[dict[str, Any]], exc: BaseException, away: _Away, extra_values: list[str]
) -> str:
    [event] = _refusals(events)
    assert event["event"] == "connector_redirect_not_followed_cross_origin"
    assert (event["from_url"], event["location"]) == ("login.example.test", _OTHER_HOST)
    assert event["reason"] == "other host"
    answer = result_connector_error("kinddemo.login", exc, 1.0)
    message = answer.extras["exception_message"]
    _assert_host_names_only(
        {
            "error": answer.error or "",
            "exception_message": message,
            "log events": repr(_refusals(events)),
        },
        [*away.values, *extra_values],
    )
    return str(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_the_shared_session_login_follows_the_same_origin_and_ends_on_another_host(
    on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    hop_path, hop_query = _made_up(), _made_up()
    hop = f"/{hop_path}?q={hop_query}"
    away = _Away()
    client = _SameOriginRedirectClient(base_url=_LOGIN_BASE)
    client.change_rule = True
    with respx.mock(assert_all_called=False) as mock:
        login, again, elsewhere = _login_routes(mock, hop, away)
        with capture_logs() as events, pytest.raises(SessionLoginError) as caught:
            await vcf_session_login(
                client,
                _LOGIN_PATH,
                username="svc",
                password="not-a-password",
                target_name="kind-demo",
                token_extractor=lambda resp: resp.headers.get("x-token", ""),
            )
    await client.aclose()

    # The login (a POST) followed the 307 to another path on the same origin.
    assert (login.call_count, again.call_count, elsewhere.call_count) == (1, 1, 0)
    message = _check_login_refusal(events, caught.value, away, [hop_path, hop_query])
    if on:
        assert type(caught.value) is SessionLoginRedirectError
        assert message == (
            "The login of this connector got HTTP 302, a redirect. "
            "MEHO did not follow it, so the login did not finish."
        )
    else:
        assert message == (
            f"vcf session-login failed for target 'kind-demo': POST {_LOGIN_PATH} returned HTTP 302"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_profiled_login_follows_the_same_origin_and_ends_on_another_host(
    on: bool, answer_switch: Any
) -> None:
    answer_switch(on)
    hop_path, hop_query = _made_up(), _made_up()
    hop = f"/{hop_path}?q={hop_query}"
    away = _Away()
    connector = _profiled_connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="kind-demo", host="login.example.test")
    with respx.mock(assert_all_called=False) as mock:
        login, again, elsewhere = _login_routes(mock, hop, away)
        operation = mock.put(f"{_LOGIN_BASE}/api/v2/items/7").respond(200, json={})
        with capture_logs() as events, pytest.raises(Exception) as caught:
            await connector._post_json(
                target,
                "/api/v2/items/7",
                operator=_profiled_operator(),
                verb="PUT",
                json={"n": 1},
            )
    await connector.aclose()

    assert (login.call_count, again.call_count, elsewhere.call_count) == (1, 1, 0)
    assert operation.call_count == 0
    message = _check_login_refusal(events, caught.value, away, [hop_path, hop_query])
    if on:
        assert type(caught.value) is SessionLoginRedirectError
        assert message == (
            "The login of this connector got HTTP 302, a redirect. "
            "MEHO did not follow it, so the login did not finish."
        )
    else:
        assert type(caught.value) is httpx.HTTPStatusError
        assert message == (
            f"Redirect response 302 from login.example.test to {_OTHER_HOST}, not followed."
        )


def _profiled_operator() -> Operator:
    return Operator(
        sub="op-kind-demo",
        name="Kind Demo Operator",
        email=None,
        raw_jwt="op.kind.demo.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s
