# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A redirect MEHO does not follow: the answer names only host names.

A small typed HTTP connector runs one read operation through the real
dispatcher, with the tenant's trace on, against a local HTTP server. The
server answers with a redirect. Each case puts made-up values into the parts
of the ``Location`` address the vendor chooses: the path, the query, the
fragment, the user part and the port text. Most cases first send MEHO
through one followed redirect on the same server, whose address also holds
made-up values.

The tests check the answer, the audit row, the stored trace and its spans,
and the log output at DEBUG and above. None of them holds a made-up value.
The answer still holds the HTTP status and the host name of the address.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import meho_backplane.flight_recorder.agent_read as agent_read_mod
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.log_host import (
    UNPARSEABLE_ADDRESS,
    UNREADABLE_LOCATION_MESSAGE,
)
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.adapters.http import _SameOriginRedirectClient
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder.capture import FIRST_REQUEST_URL_EXTENSION
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000d302")
_PRODUCT = "redirdemo"
_VERSION = "1.x"
_IMPL_ID = "redirdemo-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_OP_ID = "redirdemo.thing.get"
_START = "/api/thing"
_OTHER_HOST = "other.test.invalid"


def _made_up() -> str:
    """A made-up value no other hide rule knows: "zq" and 28 random hex characters."""
    return "zq" + secrets.token_hex(14)


class _DemoConnector(HttpConnector):
    """A typed connector whose one read operation runs through the shared HTTP seam."""

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


@dataclass
class _Server:
    server: asyncio.AbstractServer
    port: int
    hits: list[str] = field(default_factory=list)


async def _start_server(
    routes: dict[str, dict[str, str]],
    *,
    status: str = "302 Found",
    short_body: frozenset[str] = frozenset(),
    bodies: dict[str, tuple[str, str]] | None = None,
) -> _Server:
    """A local HTTP/1.1 server; each route (path with query) answers *status* with its headers.

    A path in *short_body* answers a 200 whose body ends before its
    ``Content-Length``. A path in *bodies* sends that content type and body.
    """
    state = _Server(server=None, port=0)  # type: ignore[arg-type]

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = (await reader.readline()).decode("latin-1").split()
            path = parts[1] if len(parts) > 1 else "/"
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            state.hits.append(path)
            if path in short_body:
                head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                writer.write(f'{head}Content-Length: 100\r\n\r\n{{"a":'.encode("latin-1"))
                await writer.drain()
                return
            headers = routes.get(path)
            line = status if headers is not None else "404 Not Found"
            content_type, body = (bodies or {}).get(path, ("text/plain", ""))
            data = body.encode()
            head = f"HTTP/1.1 {line}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n"
            head += f"Content-Type: {content_type}\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
            writer.write((head + "\r\n").encode("latin-1") + data)
            await writer.drain()
        finally:
            writer.close()

    state.server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    state.port = state.server.sockets[0].getsockname()[1]
    return state


@dataclass
class _Target:
    """A plain-HTTP target on the local server."""

    port: int
    product: str = _PRODUCT
    version: str = _VERSION
    host: str = "127.0.0.1"
    name: str = "redirect-demo"
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


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_DemoConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


class _MemoryValkey:
    """An in-memory stand-in for the result-handle store's Valkey client."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)

    def text(self) -> str:
        return b"".join(self.values.values()).decode("utf-8", "replace")


_AGENT_STORE = _MemoryValkey()


@pytest.fixture(autouse=True)
def _agent_trace_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent's copy of the trace goes to an in-memory store the tests read."""
    _AGENT_STORE.values.clear()
    store = ResultHandleStore(_AGENT_STORE)  # type: ignore[arg-type]
    monkeypatch.setattr(agent_read_mod, "get_result_handle_store", lambda: store)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _operator() -> Operator:
    # A person in the web console.
    return Operator(
        sub="op-redirect-demo",
        name="Redirect Demo Operator",
        email=None,
        raw_jwt="op.redirect.demo.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        web_session=True,
    )


async def _register(session: AsyncSession, *, trace: bool = True) -> None:
    await register_typed_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        handler=_DemoConnector.thing_get,
        summary="Read one thing.",
        description="Synthetic read of one thing.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        tags=["read"],
        safety_level="safe",
        requires_approval=False,
    )
    session.add(
        Tenant(
            id=_TENANT_ID, slug="redirect-demo", name="redirect-demo", flight_recorder_enabled=trace
        )
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


def _row_text(row: Any) -> str:
    return json.dumps(
        {column.name: getattr(row, column.key) for column in row.__mapper__.columns},
        default=str,
    )


async def _stored_text(audit_id: UUID, *, trace_on: bool = True) -> tuple[str, str]:
    """All columns of the audit row, and of the stored trace and its spans."""
    async with get_sessionmaker()() as s:
        audit = (await s.execute(select(AuditLog).where(AuditLog.id == audit_id))).scalar_one()
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one_or_none()
        assert (trace is not None) is trace_on
        if trace is None:
            return _row_text(audit), ""
        spans = (
            (
                await s.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == trace.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(span.span_kind == "vendor_call" for span in spans)
        trace_text = _row_text(trace) + "".join(_row_text(span) for span in spans)
        return _row_text(audit), trace_text


async def _run(
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    routes: dict[str, dict[str, str]],
    *,
    trace: bool = True,
    **server_options: Any,
) -> tuple[Any, dict[str, str], list[dict[str, Any]]]:
    """Dispatch the operation; return the result, each place as text, and the log events."""
    await _register(session, trace=trace)
    server = await _start_server(routes, **server_options)
    caplog.set_level(logging.DEBUG)
    try:
        with capture_logs() as events:
            result = await dispatch(
                operator=_operator(),
                connector_id=_CONNECTOR_ID,
                op_id=_OP_ID,
                target=_Target(port=server.port),
                params={},
            )
    finally:
        server.server.close()
        await server.server.wait_closed()
    assert result.audit_id is not None
    audit_text, trace_text = await _stored_text(result.audit_id, trace_on=trace)
    places = {
        "answer": result.model_dump_json(),
        "audit row": audit_text,
        "stored trace": trace_text,
        "log events": repr(events),
        "log records": caplog.text,
        "agent copy of the trace": _AGENT_STORE.text(),
    }
    return result, places, events


def _assert_absent(values: list[str], places: dict[str, str]) -> None:
    for place, text in places.items():
        for value in values:
            assert value not in text, f"a made-up value is in the {place}"


def _refusal_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(e for e in events if e["event"] == "connector_redirect_not_followed_cross_origin")


@pytest.mark.asyncio
async def test_refused_redirect_after_a_followed_hop_names_only_host_names(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    hop_path, hop_query = _made_up(), _made_up()
    user, password, path, query, fragment = (_made_up() for _ in range(5))
    hop = f"/{hop_path}?q={hop_query}"
    location = f"http://{user}:{password}@{_OTHER_HOST}/{path}?q={query}#{fragment}"
    routes = {_START: {"Location": hop}, hop: {"Location": location}}

    result, places, events = await _run(session, caplog, routes)

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["http_status"] == 302
    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert _refusal_event(events)["reason"] == "other host"
    _assert_absent([hop_path, hop_query, user, password, path, query, fragment], places)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority", "host", "reason"),
    [
        ("[::1]:8443", "::1", "other host"),
        ("127.0.0.1:1", "127.0.0.1", "other port"),
    ],
)
async def test_refused_redirect_names_only_the_host_name(
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    authority: str,
    host: str,
    reason: str,
) -> None:
    path, query, fragment = _made_up(), _made_up(), _made_up()
    routes = {_START: {"Location": f"http://{authority}/{path}?q={query}#{fragment}"}}

    result, places, events = await _run(session, caplog, routes)

    assert result.extras["http_status"] == 302
    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {host}, not followed."
    )
    assert _refusal_event(events)["reason"] == reason
    _assert_absent([path, query, fragment], places)


@pytest.mark.asyncio
async def test_redirect_to_https_on_the_same_host_names_the_reason(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    path = _made_up()
    routes = {_START: {"Location": f"https://127.0.0.1/{path}"}}

    result, places, events = await _run(session, caplog, routes)

    assert result.extras["exception_message"] == (
        "Redirect response 302 from 127.0.0.1 to 127.0.0.1, not followed."
    )
    assert _refusal_event(events)["reason"] == "other scheme"
    _assert_absent([path], places)


@pytest.mark.asyncio
async def test_unreadable_port_after_a_followed_hop_gives_one_fixed_text(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    hop_path, port, path, query = _made_up(), _made_up(), _made_up(), _made_up()
    hop = f"/{hop_path}"
    routes = {
        _START: {"Location": hop},
        hop: {"Location": f"http://{_OTHER_HOST}:{port}/{path}?q={query}"},
    }

    result, places, events = await _run(session, caplog, routes)

    assert result.status == "error"
    assert result.extras["exception_message"] == UNREADABLE_LOCATION_MESSAGE
    assert _refusal_event(events)["reason"] == "unreadable address"
    _assert_absent([hop_path, port, path, query], places)


def test_redirect_without_location_names_only_the_host() -> None:
    """A 3xx without a Location header: the text has the status and the request host only."""
    path, query = _made_up(), _made_up()
    request = httpx.Request("GET", f"https://appliance.test.invalid/{path}?q={query}")
    response = httpx.Response(304, request=request)
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()

    result = result_connector_error(_OP_ID, error.value, 1.0)

    assert result.extras["exception_message"] == (
        "Redirect response 304 from appliance.test.invalid, with no Location header."
    )
    assert result.extras["http_status"] == 304
    _assert_absent([path, query], {"answer": result.model_dump_json()})


# The vendor's words around each address in a 3xx body. Each ``{}`` is one
# way to write the address.
_BODY_WORDS = (
    "The document has moved to {}. Old link: {}; next {} (this one). "
    "Escaped {} and {}, encoded {} or {}. Please sign in again."
)


def _address_forms(location: str, relative: str) -> list[str]:
    """The ways a vendor body writes ``location`` and its part from the path on."""
    double_slash = location[location.index("//") :]
    return [
        location,
        double_slash,
        relative,
        location.replace("&", "&amp;"),
        relative.replace("&", "&amp;"),
        quote(location, safe=""),
        quote(relative, safe=""),
    ]


def _redirect_body(kind: str, words: str) -> tuple[str, str]:
    """A 3xx body of one kind, with ``words`` as its text."""
    if kind == "html link":
        link = words.split(" ")[5].rstrip(".")
        html = f'<html><body><p>Found. <a href="{link}">here</a></p><p>{words}</p></body></html>'
        return "text/html", html
    if kind == "json message":
        return "application/json", json.dumps({"message": words})
    if kind == "json other key":
        return "application/json", json.dumps({"detail": words, "status": 302})
    return "text/plain", words


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["html link", "json message", "json other key", "plain text"])
async def test_refused_redirect_body_names_each_address_only_by_its_host_name(
    session: AsyncSession, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """Each address in the vendor text of a 3xx shows only its host name; the words stay."""
    user, password, path, query, ticket, fragment = (_made_up() for _ in range(6))
    port = random.randint(20000, 60999)
    relative = f"/{path}?q={query}&ticket={ticket}#{fragment}"
    location = f"http://{user}:{password}@{_OTHER_HOST}:{port}{relative}"
    forms = _address_forms(location, relative)
    content_type, body = _redirect_body(kind, _BODY_WORDS.format(*forms))
    _, expected = _redirect_body(kind, _BODY_WORDS.format(*[_OTHER_HOST] * len(forms)))
    if kind == "json message":
        expected = json.loads(expected)["message"]
    routes = {_START: {"Location": location}}

    result, places, _ = await _run(
        session, caplog, routes, trace=False, bodies={_START: (content_type, body)}
    )

    assert result.extras["http_status"] == 302
    assert result.extras["upstream_message"] == expected
    assert json.dumps(expected) in places["audit row"]
    made_up = [user, password, path, query, ticket, fragment, f":{port}"]
    _assert_absent(made_up, {"answer": places["answer"], "audit row": places["audit row"]})


def test_relative_location_in_a_3xx_body_gives_the_fixed_mark() -> None:
    """A relative Location in the body, in each form, becomes the fixed mark."""
    path, query, ticket, fragment = (_made_up() for _ in range(4))
    location = f"/{path}?q={query}&ticket={ticket}#{fragment}"
    forms = [location, location.replace("&", "&amp;"), quote(location, safe="")]
    words = "Moved to {}, or {} and {}. Sign in again."
    request = httpx.Request("GET", f"https://appliance.test.invalid{_START}")
    response = httpx.Response(
        302, request=request, headers={"Location": location}, text=words.format(*forms)
    )
    error = httpx.HTTPStatusError("302", request=request, response=response)

    result = result_connector_error(_OP_ID, error, 1.0)

    assert result.extras["upstream_message"] == words.format(*[UNPARSEABLE_ADDRESS] * 3)
    _assert_absent([path, query, ticket, fragment], {"answer": result.model_dump_json()})


def test_upstream_message_of_a_non_redirect_answer_keeps_its_addresses() -> None:
    """Only a 3xx answer reduces addresses; a 500 keeps the vendor text as it is."""
    text = "Upstream failed at https://appliance.test.invalid:8443/api/x?q=1, try later."
    request = httpx.Request("GET", f"https://appliance.test.invalid{_START}")
    response = httpx.Response(500, request=request, text=text)
    error = httpx.HTTPStatusError("500", request=request, response=response)

    result = result_connector_error(_OP_ID, error, 1.0)

    assert result.extras["upstream_message"] == text


@pytest.mark.asyncio
async def test_followed_hop_answer_names_the_first_address_for_the_trace() -> None:
    """After a followed hop, the answer carries the address MEHO asked for, for the trace."""
    hop = f"/{_made_up()}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _START:
            return httpx.Response(302, headers={"Location": hop})
        return httpx.Response(200, json={})

    async with _SameOriginRedirectClient(
        base_url="https://appliance.test.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        followed = await client.get(_START)
        direct = await client.get(hop)

    assert followed.extensions[FIRST_REQUEST_URL_EXTENSION] == httpx.URL(
        f"https://appliance.test.invalid{_START}"
    )
    assert FIRST_REQUEST_URL_EXTENSION not in direct.extensions


@pytest.mark.asyncio
async def test_followed_hop_whose_body_ends_early_names_only_the_first_address(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    hop_path, hop_query = _made_up(), _made_up()
    hop = f"/{hop_path}?q={hop_query}"
    routes = {_START: {"Location": hop}}

    result, places, _events = await _run(
        session, caplog, routes, status="307 Temporary Redirect", short_body=frozenset({hop})
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_timeout"
    assert _AGENT_STORE.values, "the agent got no copy of the trace"
    assert f'{_START}"' in places["stored trace"]
    _assert_absent([hop_path, hop_query], places)
