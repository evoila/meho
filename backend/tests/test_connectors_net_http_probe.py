# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``net.http_probe`` — #2408 (Initiative #2405).

Covers the T3 probe's contract on top of the T1 (#2406) net.* mold:

* A fresh-boot, targetless dispatch returns status/headers/redirect_chain
  /timing for a real loopback HTTP server — with **no ``body`` key**
  anywhere in the result (grep-pinned) and ``body_size`` / ``body_sha256``
  present (anti-exfil floor).
* Every redirect hop is re-gated against ``MEHO_NETDIAG_PROBE_ALLOWLIST``:
  a redirect to a non-allowlisted host halts with ``blocked_redirect``
  (``status="ok"``) and the redirect target is **never dialed** — pinned
  with a second local server whose hit-flag must stay false.
* The initial ``url`` host is allowlist-gated; the **final** URL lands in
  the durable audit row's ``raw_payload``.
* A connection failure returns ``{reachable: false, reason}`` with
  ``status="ok"`` — never a ``connector_*`` error.
* ``method`` rejects anything but HEAD/GET at the schema boundary.

The autouse ``_default_database_url`` conftest fixture migrates the
SQLite DB to head so the descriptor / audit tables exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import http_probe as net_http_probe_mod
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.http_probe import (
    net_http_probe,
    register_net_http_probe_operations,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.http_probe"


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the minimal Settings env + reset dispatcher caches per test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_http_probe_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the ``net.http_probe`` descriptor row for dispatch-driving tests."""
    await register_net_http_probe_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_probe(params: dict[str, Any]) -> OperationResult:
    """Dispatch ``net.http_probe`` through the real targetless path."""
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Minimal loopback HTTP/1.1 server: canned responses keyed by path
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass
class _TestServer:
    server: asyncio.AbstractServer
    host: str
    port: int
    hits: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"


async def _start_http_server(
    routes: dict[str, _Route],
    *,
    host: str = "127.0.0.1",
) -> _TestServer:
    """Start a throwaway HTTP/1.1 server serving *routes* keyed by path.

    Records each requested path in ``.hits`` (so a test can assert a
    server was — or was never — dialed). Responds ``Connection: close``
    so httpx opens a fresh connection per hop.
    """
    state = _TestServer(server=None, host=host, port=0)  # type: ignore[arg-type]

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"
            # Drain request headers.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            state.hits.append(path)
            route = routes.get(path, _Route(status=404, body=b"not found"))
            headers = dict(route.headers)
            headers.setdefault("Content-Length", str(len(route.body)))
            headers["Connection"] = "close"
            head = f"HTTP/1.1 {route.status} X\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            head += "\r\n"
            payload = head.encode("latin-1")
            if method != "HEAD":
                payload += route.body
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, host, 0)
    state.server = server
    state.port = server.sockets[0].getsockname()[1]
    return state


# ---------------------------------------------------------------------------
# Synthetic identity
# ---------------------------------------------------------------------------


def test_http_probe_connector_id_round_trips() -> None:
    """The wire connector_id resolves to the registered natural key."""
    assert parse_connector_id(_CONNECTOR_ID) == ("net", "1.x", "net-probe")


async def test_http_probe_registered_as_safe_ungated_typed_op(
    _registered_http_probe_op: None,
) -> None:
    """The descriptor row carries the synthetic identity + safe/ungated posture."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "net",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "net-probe",
                EndpointDescriptor.op_id == _OP_ID,
            )
        )
        row = result.scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False


def test_http_probe_module_registers_no_connector_class() -> None:
    """``net`` stays synthetic — no ``register_connector`` in the new module."""
    source = Path(net_http_probe_mod.__file__).read_text()
    assert "register_connector_v2(" not in source
    assert "register_connector(" not in source


# ---------------------------------------------------------------------------
# Happy path: status/headers/redirect_chain/timing, NO body key
# ---------------------------------------------------------------------------


async def test_probe_reports_surface_and_never_returns_a_body(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """Fresh-boot targetless GET → status/headers/timing, no ``body`` key."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    srv = await _start_http_server(
        {"/health": _Route(status=200, headers={"X-App": "meho"}, body=b"hello-body")}
    )
    try:
        result = await _dispatch_probe({"url": f"{srv.origin}/health", "method": "GET"})
    finally:
        srv.server.close()
        await srv.server.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    # Anti-exfil floor: the response body is NEVER a key of the result.
    assert "body" not in body
    assert body["reachable"] is True
    assert body["reason"] is None
    assert body["status"] == 200
    assert body["headers"]["x-app"] == "meho"
    assert body["redirect_chain"] == []
    assert isinstance(body["timing_ms"], float)
    assert body["tls"] is None  # plain HTTP
    assert body["body_size"] == len(b"hello-body")
    assert body["body_sha256"] == hashlib.sha256(b"hello-body").hexdigest()
    assert body["final_url"] == f"{srv.origin}/health"


async def test_head_probe_reports_no_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """A HEAD probe (the default) reports headers/status with a 0-byte body."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    srv = await _start_http_server(
        {"/": _Route(status=204, headers={"X-Kind": "head"}, body=b"ignored-on-head")}
    )
    try:
        result = await _dispatch_probe({"url": f"{srv.origin}/"})
    finally:
        srv.server.close()
        await srv.server.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    assert "body" not in body
    assert body["status"] == 204
    assert body["headers"]["x-kind"] == "head"
    assert body["body_size"] == 0


# ---------------------------------------------------------------------------
# Redirect re-gating — the SSRF floor
# ---------------------------------------------------------------------------


async def test_same_host_redirect_is_followed_to_terminal(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """A redirect to an allowlisted host is followed; the chain is recorded."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    srv = await _start_http_server(
        {
            "/start": _Route(status=302, headers={"Location": "/final"}),
            "/final": _Route(status=200, body=b"arrived"),
        }
    )
    try:
        result = await _dispatch_probe({"url": f"{srv.origin}/start", "method": "GET"})
    finally:
        srv.server.close()
        await srv.server.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    assert body["reachable"] is True
    assert body["reason"] is None
    assert body["status"] == 200
    assert [hop["status"] for hop in body["redirect_chain"]] == [302]
    assert body["redirect_chain"][0]["url"] == f"{srv.origin}/start"
    assert body["final_url"] == f"{srv.origin}/final"
    assert "/start" in srv.hits and "/final" in srv.hits


async def test_redirect_to_non_allowlisted_host_is_blocked_and_never_dialed(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """A redirect off the allowlist halts with blocked_redirect, target undialed.

    Two servers: ``localhost`` (allowlisted) returns a 302 whose Location
    points at the ``127.0.0.1`` server (NOT allowlisted — verbatim
    hostname allowlist has no IP entry). The metadata/credential server's
    hit-list must stay empty: the re-gate refuses it before any socket.
    """
    metadata = await _start_http_server(
        {"/creds": _Route(status=200, body=b"SECRET")}, host="127.0.0.1"
    )
    entry = await _start_http_server(
        {
            "/go": _Route(
                status=302,
                headers={"Location": f"http://127.0.0.1:{metadata.port}/creds"},
            )
        },
        host="127.0.0.1",
    )
    # Allowlist only the hostname 'localhost' — the entry server is dialed
    # as localhost; the redirect's 127.0.0.1 target is an IP not covered.
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "localhost")
    try:
        result = await _dispatch_probe(
            {"url": f"http://localhost:{entry.port}/go", "method": "GET"}
        )
    finally:
        for srv in (entry, metadata):
            srv.server.close()
            await srv.server.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    assert body["reachable"] is True
    assert body["reason"] == "blocked_redirect"
    assert body["blocked_redirect"] == "127.0.0.1"
    assert [hop["status"] for hop in body["redirect_chain"]] == [302]
    # The credential host was NEVER dialed.
    assert metadata.hits == []


# ---------------------------------------------------------------------------
# Initial-host gating + audit row records the final URL
# ---------------------------------------------------------------------------


async def test_initial_host_outside_allowlist_refused_before_socket(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """Empty/uncovered allowlist ⇒ structured refusal, no socket opened."""

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("no HTTP request may run when the initial host is refused")

    monkeypatch.setattr(net_http_probe_mod.httpx.AsyncClient, "send", _boom)
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")

    result = await _dispatch_probe({"url": "http://192.168.1.5/health"})
    assert result.status == "ok", result.error
    assert result.result["reachable"] is False
    assert result.result["reason"] == "not_in_probe_allowlist"
    assert result.result["status"] is None


async def test_audit_row_records_url_and_final_url(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """The durable audit row's raw_payload carries url + final_url."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    srv = await _start_http_server(
        {
            "/a": _Route(status=301, headers={"Location": "/b"}),
            "/b": _Route(status=200, body=b"ok"),
        }
    )
    try:
        result = await _dispatch_probe({"url": f"{srv.origin}/a", "method": "GET"})
    finally:
        srv.server.close()
        await srv.server.wait_closed()
    assert result.status == "ok", result.error

    rows = await _fetch_audit_rows()
    probe_rows = [r for r in rows if r.path == _OP_ID]
    assert len(probe_rows) == 1
    raw = probe_rows[0].raw_payload
    assert raw is not None
    assert raw["url"] == f"{srv.origin}/a"
    assert raw["final_url"] == f"{srv.origin}/b"
    assert "body" not in raw
    assert f"{srv.origin}/b" in json.dumps(raw)


# ---------------------------------------------------------------------------
# Return-failures contract
# ---------------------------------------------------------------------------


async def test_connection_refused_is_ok_status_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """A refused connect (closed port) → reachable=false, reason, status=ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    # Bind then release a port so it is (almost certainly) closed now.
    srv = await _start_http_server({"/": _Route(status=200)})
    port = srv.port
    srv.server.close()
    await srv.server.wait_closed()

    result = await _dispatch_probe({"url": f"http://127.0.0.1:{port}/"})

    assert result.status == "ok", result.error
    assert result.extras.get("exception_class") is None
    body = result.result
    assert body["reachable"] is False
    assert body["reason"] in {"refused", "unreachable"}
    assert body["status"] is None


@pytest.mark.parametrize(
    "url,expected_reason",
    [
        ("ftp://127.0.0.1/x", "invalid_url"),
        ("not-a-url", "invalid_url"),
    ],
)
async def test_invalid_url_is_ok_status_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected_reason: str,
) -> None:
    """A non-http(s)/malformed URL returns a structured refusal, never raises."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await net_http_probe(_make_operator(), None, {"url": url})
    assert result["reachable"] is False
    assert result["reason"] == expected_reason
    assert "body" not in result


async def test_timeout_maps_to_reason_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow probe past the deadline → reachable=false, reason='timeout'."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.9")

    async def _hang(*_a: object, **_kw: object) -> object:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    monkeypatch.setattr(net_http_probe_mod.httpx.AsyncClient, "send", _hang)
    result = await net_http_probe(
        _make_operator(),
        None,
        {"url": "http://203.0.113.9/x", "timeout_seconds": 0.05},
    )
    assert result["reachable"] is False
    assert result["reason"] == "timeout"


# ---------------------------------------------------------------------------
# Schema boundary — method enum
# ---------------------------------------------------------------------------


async def test_method_enum_rejects_non_head_get_at_schema_boundary(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """A method other than HEAD/GET is rejected by the dispatcher's validator."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await _dispatch_probe({"url": "http://127.0.0.1/x", "method": "POST"})
    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"
