# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the tests of a redirect in an answer of a typed HTTP connector.

A small typed HTTP connector runs one read operation through the real
dispatcher against a local HTTP server. :func:`_run` dispatches it and
returns each place the answer can reach as text: the answer, every column
of the audit row, the stored trace and its spans, the agent's copy of the
trace, and the log output at DEBUG and above.

The test modules import the autouse fixtures by name, so they apply there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import meho_backplane.flight_recorder.agent_read as agent_read_mod
import meho_backplane.logging as meho_logging
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
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


def _ticket() -> tuple[str, list[str]]:
    """A made-up one-time value in the address, and its made-up parts."""
    user, hex_part, signature = _made_up(), secrets.token_hex(4).upper(), _made_up()
    return f"PVE:{user}@pve:{hex_part}::{signature}", [user, hex_part, signature]


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
    route_status: dict[str, str] | None = None,
    route_body: dict[str, bytes] | None = None,
) -> _Server:
    """A local HTTP/1.1 server; each route (path with query) answers *status* with its headers.

    *route_status* and *route_body* set the status line and the body of one
    route. The text ``{port}`` in a header value becomes the server's port.
    A path in *short_body* answers a 200 whose body ends before its
    ``Content-Length``.
    """
    state = _Server(server=None, port=0)  # type: ignore[arg-type]
    statuses = route_status or {}
    bodies = route_body or {}

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
            line = statuses.get(path, status if headers is not None else "404 Not Found")
            body = bodies.get(path, b"")
            head = f"HTTP/1.1 {line}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
            port = str(state.port)
            head += "".join(
                f"{k}: {v.replace('{port}', port)}\r\n" for k, v in (headers or {}).items()
            )
            writer.write((head + "\r\n").encode("latin-1") + body)
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
def _httpx_logger_on() -> Iterator[None]:
    """Turn the ``httpx`` logger on with MEHO's filter, as in a server, and put it back after.

    The database set-up of the tests runs alembic, which turns off every
    logger that exists. In a server the migrations run in a separate job,
    so the ``httpx`` request line reaches the log.
    """
    logger = logging.getLogger("httpx")
    saved = (logger.level, logger.disabled, logger.filters[:])
    logger.disabled = False
    logger.setLevel(logging.NOTSET)
    logger.addFilter(meho_logging._HTTPX_URL_FILTER)
    try:
        yield
    finally:
        logger.level, logger.disabled, logger.filters = saved[0], saved[1], saved[2]


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


async def _stored_text(audit_id: UUID, *, trace: bool = True) -> tuple[str, str]:
    """All columns of the audit row, and of the stored trace and its spans.

    With the tenant's trace off, no trace is stored and the second text is empty.
    """
    async with get_sessionmaker()() as s:
        audit = (await s.execute(select(AuditLog).where(AuditLog.id == audit_id))).scalar_one()
        traces = (
            (await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id)))
            .scalars()
            .all()
        )
        if not trace:
            assert not traces
            return _row_text(audit), ""
        (stored,) = traces
        spans = (
            (
                await s.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == stored.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(span.span_kind == "vendor_call" for span in spans)
        trace_text = _row_text(stored) + "".join(_row_text(span) for span in spans)
        return _row_text(audit), trace_text


async def _vendor_call_bodies(audit_id: UUID) -> list[Any]:
    """The ``response_body`` of each ``vendor_call`` span of the stored trace."""
    async with get_sessionmaker()() as s:
        trace = await s.scalar(select(DispatchTrace).filter_by(audit_id=audit_id))
        assert trace is not None
        spans = await s.scalars(
            select(DispatchTraceSpan).filter_by(trace_id=trace.id, span_kind="vendor_call")
        )
        return [span.attributes.get("response_body") for span in spans]


async def _run(
    caplog: pytest.LogCaptureFixture,
    routes: dict[str, dict[str, str]],
    *,
    trace: bool = True,
    **server_options: Any,
) -> tuple[Any, dict[str, str], list[dict[str, Any]]]:
    """Dispatch the operation; return the result, each place as text, and the log events."""
    async with get_sessionmaker()() as session:
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
    audit_text, trace_text = await _stored_text(result.audit_id, trace=trace)
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
