# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``proxmox.about`` names only host names when one of its requests gets a redirect.

The operation runs through the real dispatcher against a local HTTP server.

- The server answers the ticket login with a 302 to another host. The
  ``Location`` address holds made-up values in the user part, the port, the
  path, the query and the fragment. The answer, the audit row and the stored
  trace hold none of them.
- The ticket login, the ``GET /version`` or the ``GET /nodes`` read gets a
  307 to the same host, and the next answer is a 404, a 429 or a 500. The
  address holds made-up values in the path, the query (also a made-up
  one-time value in the address), the fragment and the user part. With the
  tenant's trace on and off, ``extras.exception_message`` names only the
  status and the host name, and no place the answer reaches holds a made-up
  value: the answer, every column of the audit row, the stored trace and its
  spans, the agent's copy of the trace, and the log output at DEBUG.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.proxmox import ProxmoxConnector
from meho_backplane.connectors.proxmox.session import ProxmoxCredentials
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target
from tests._http_redirect_harness import (  # noqa: F401 -- the autouse fixture applies by import
    _AGENT_STORE,
    _agent_trace_store,
    _assert_absent,
    _httpx_logger_on,
    _ticket,
)

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000d303")
_TARGET_NAME = "proxmox-redirect-demo"
_OTHER_HOST = "other.test.invalid"
_OPERATOR = Operator(
    sub="proxmox-redirect-demo",
    name="Proxmox Redirect Demo Operator",
    email=None,
    raw_jwt="<proxmox-redirect-demo-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)


def _made_up() -> str:
    """A made-up value no other hide rule knows: "zq" and 28 random hex characters."""
    return "zq" + secrets.token_hex(14)


def _ticket_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> ProxmoxCredentials:
        return ProxmoxCredentials(mode="ticket", username="demo@pve", password="not-a-password")

    return _load()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_flight_recorder_config_cache_for_testing()
    yield
    reset_dispatcher_caches()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


async def _start_server(location: str) -> asyncio.AbstractServer:
    """A local HTTP/1.1 server that answers every request with a 302 to *location*."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            length = 0
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            await reader.readexactly(length)
            writer.write(
                (
                    "HTTP/1.1 302 Found\r\nContent-Length: 0\r\nConnection: close\r\n"
                    f"Location: {location}\r\n\r\n"
                ).encode("latin-1")
            )
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(_handle, "127.0.0.1", 0)


@pytest.fixture
async def connector() -> AsyncIterator[ProxmoxConnector]:
    await ProxmoxConnector.register_operations()
    instance = ProxmoxConnector(credentials_loader=_ticket_loader)
    _CONNECTOR_INSTANCE_CACHE[ProxmoxConnector] = instance
    yield instance
    await instance.aclose()
    _CONNECTOR_INSTANCE_CACHE.pop(ProxmoxConnector, None)


async def _seed(port: int, *, trace: bool = True) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT_ID,
                slug="proxmox-redirect-demo",
                name="proxmox-redirect-demo",
                flight_recorder_enabled=trace,
            )
        )
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
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
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()


def _row_text(row: Any) -> str:
    return json.dumps(
        {column.name: getattr(row, column.key) for column in row.__mapper__.columns},
        default=str,
    )


async def _stored_text(audit_id: uuid.UUID) -> dict[str, str]:
    """The audit row, and the stored trace with its spans, each as text."""
    async with get_sessionmaker()() as s:
        audit = await s.get(AuditLog, audit_id)
        trace = await s.scalar(select(DispatchTrace).filter_by(audit_id=audit_id))
        assert audit is not None
        assert trace is not None
        spans = list(await s.scalars(select(DispatchTraceSpan).filter_by(trace_id=trace.id)))
    assert spans
    trace_text = _row_text(trace) + "".join(map(_row_text, spans))
    return {"audit row": _row_text(audit), "stored trace": trace_text}


@pytest.mark.asyncio
async def test_about_with_a_redirected_ticket_login_names_only_host_names(
    connector: ProxmoxConnector,
) -> None:
    user, password, path, query, fragment = (_made_up() for _ in range(5))
    port = str(10000 + secrets.randbelow(55000))
    location = f"http://{user}:{password}@{_OTHER_HOST}:{port}/{path}?q={query}#{fragment}"
    server = await _start_server(location)
    try:
        await _seed(server.sockets[0].getsockname()[1])
        async with get_sessionmaker()() as session:
            target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
        result = await dispatch(
            operator=_OPERATOR,
            connector_id="proxmox-api-8.x",
            op_id="proxmox.about",
            target=target,
            params={},
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "error"
    assert result.extras["exception_message"] == (
        f"HTTPStatusError: Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert result.audit_id is not None
    places = {"answer": result.model_dump_json(), **(await _stored_text(result.audit_id))}
    for place, text in places.items():
        for value in (user, password, port, path, query, fragment):
            assert value not in text, f"a made-up value is in the {place}"


_TICKET_PATH = "/api2/json/access/ticket"
_VERSION_PATH = "/api2/json/version"
_NODES_PATH = "/api2/json/nodes"
_LOGIN_BODY = b'{"data": {"ticket": "demo-ticket", "CSRFPreventionToken": "demo-csrf"}}'
_JSON = {"Content-Type": "application/json"}
_OK_ROUTES: dict[str, tuple[str, dict[str, str], bytes]] = {
    _TICKET_PATH: ("200 OK", _JSON, _LOGIN_BODY),
    _VERSION_PATH: ("200 OK", _JSON, b'{"data": {"version": "8.2"}}'),
    _NODES_PATH: ("200 OK", _JSON, b'{"data": []}'),
}


async def _start_routed_server(
    routes: dict[str, tuple[str, dict[str, str], bytes]],
) -> asyncio.AbstractServer:
    """A local HTTP/1.1 server; each path (with query) answers its status line, headers, body.

    The text ``{port}`` in a header value becomes the server's port.
    """
    port_box: list[int] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = (await reader.readline()).decode("latin-1").split()
            path = parts[1] if len(parts) > 1 else "/"
            length = 0
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            await reader.readexactly(length)
            status, headers, body = routes.get(path, ("404 Not Found", {}, b""))
            head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
            head += "".join(
                f"{k}: {v.replace('{port}', str(port_box[0]))}\r\n" for k, v in headers.items()
            )
            writer.write((head + "\r\n").encode("latin-1") + body)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port_box.append(server.sockets[0].getsockname()[1])
    return server


async def _places(
    result: Any, events: list[dict[str, Any]], caplog: pytest.LogCaptureFixture, *, trace: bool
) -> dict[str, str]:
    """Each place the answer can reach, as text."""
    assert result.audit_id is not None
    async with get_sessionmaker()() as s:
        audit = await s.get(AuditLog, result.audit_id)
        assert audit is not None
        stored = await s.scalar(select(DispatchTrace).filter_by(audit_id=result.audit_id))
        trace_text = ""
        if trace:
            assert stored is not None
            spans = list(await s.scalars(select(DispatchTraceSpan).filter_by(trace_id=stored.id)))
            trace_text = _row_text(stored) + "".join(map(_row_text, spans))
        else:
            assert stored is None
    return {
        "answer": result.model_dump_json(),
        "audit row": _row_text(audit),
        "stored trace": trace_text,
        "log events": repr(events),
        "log records": caplog.text,
        "agent copy of the trace": _AGENT_STORE.text(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirected", "status_line", "text"),
    [
        (_TICKET_PATH, "404 Not Found", "Client error '404 Not Found'"),
        (_TICKET_PATH, "429 Too Many Requests", "Client error '429 Too Many Requests'"),
        (_TICKET_PATH, "500 Internal Server Error", "Server error '500 Internal Server Error'"),
        (_VERSION_PATH, "404 Not Found", "Client error '404 Not Found'"),
        (_NODES_PATH, "500 Internal Server Error", "Server error '500 Internal Server Error'"),
    ],
    ids=["login-404", "login-429", "login-500", "version-404", "nodes-500"],
)
@pytest.mark.parametrize("trace", [True, False], ids=["trace-on", "trace-off"])
async def test_about_after_a_followed_redirect_names_only_the_host(
    connector: ProxmoxConnector,
    caplog: pytest.LogCaptureFixture,
    redirected: str,
    status_line: str,
    text: str,
    trace: bool,
) -> None:
    path, user, password, fragment = (_made_up() for _ in range(4))
    ticket, ticket_parts = _ticket()
    hop = f"/{path}?ticket={ticket}"
    location = f"http://{user}:{password}@127.0.0.1:{{port}}{hop}#{fragment}"
    routes = dict(_OK_ROUTES)
    routes[redirected] = ("307 Temporary Redirect", {"Location": location}, b"")
    routes[hop] = (status_line, {"Content-Type": "text/plain"}, b"the vendor text")
    server = await _start_routed_server(routes)
    caplog.set_level(logging.DEBUG)
    try:
        await _seed(server.sockets[0].getsockname()[1], trace=trace)
        async with get_sessionmaker()() as session:
            target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
        with capture_logs() as events:
            result = await dispatch(
                operator=_OPERATOR,
                connector_id="proxmox-api-8.x",
                op_id="proxmox.about",
                target=target,
                params={},
            )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "error"
    assert result.extras["exception_message"] == (
        f"HTTPStatusError: {text} from 127.0.0.1, after a redirect on the same host."
    )
    places = await _places(result, events, caplog, trace=trace)
    assert result.extras["exception_message"] in places["audit row"]
    _assert_absent([path, user, password, fragment, *ticket_parts], places)
