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
import socket
import ssl
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import httpx
import pytest
import respx
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
    #: The verbatim ``Host:`` header seen on each hit, in order — lets a
    #: test pin which host was sent on which redirect hop.
    request_hosts: list[str] = field(default_factory=list)

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
            # Drain request headers, capturing Host for override assertions.
            request_host = ""
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, sep, value = line.decode("latin-1").partition(":")
                if sep and name.strip().lower() == "host":
                    request_host = value.strip()
            state.hits.append(path)
            state.request_hosts.append(request_host)
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
    # A clean response carries no failure evidence.
    assert body["error_detail"] is None


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

    #2784 carve-out: unlike an *initial*-host refusal (which fails the
    dispatch as ``connector_probe_refused``), this stays ``status="ok"``
    with ``reachable=true`` — the prior hop answered, so the result is a
    genuine observation, not a probe that never ran.
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
    # NOT reclassified as a dispatch error — the #2784 carve-out.
    assert result.extras.get("error_code") is None
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
    # #2784: nothing was dialed, so the *initial*-host refusal fails the
    # dispatch. (The mid-chain redirect re-gate keeps status=ok — see
    # test_redirect_to_non_allowlisted_host_is_blocked_and_never_dialed.)
    assert result.status == "error"
    assert result.result is None
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["host"] == "192.168.1.5"
    assert result.error is not None
    assert PROBE_ALLOWLIST_ENV in result.error


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
    # AC3: a connection-level failure carries the mapped exception chain
    # as bounded innermost-first {type, message} evidence.
    assert isinstance(body["error_detail"], list) and body["error_detail"]
    assert all({"type", "message"} == set(entry) for entry in body["error_detail"])


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


# ---------------------------------------------------------------------------
# Transport-failure taxonomy (#2771): classify through ExceptionGroup
# children + TLS-phase mappings instead of collapsing to 'unreachable'
# ---------------------------------------------------------------------------

_classify = net_http_probe_mod._reason_for_transport_error
_detail = net_http_probe_mod._error_detail


def _anyio_multi_connect_error(children: list[Exception]) -> httpx.ConnectError:
    """Rebuild the anyio happy-eyeballs multi-address failure shape.

    ``httpx.ConnectError ← OSError("All connection attempts failed") ←
    ExceptionGroup(children)`` — exactly what anyio 4.13 raises when more
    than one address (dual-stack A+AAAA) is tried and every attempt
    fails, then httpcore/httpx re-wrap. The per-attempt errors live in
    the group's ``.exceptions``, not on ``__cause__``.
    """
    group = ExceptionGroup("multiple connection attempts failed", children)
    os_err = OSError("All connection attempts failed")
    os_err.__cause__ = group
    connect_err = httpx.ConnectError("All connection attempts failed")
    connect_err.__cause__ = os_err
    return connect_err


@pytest.mark.parametrize(
    ("children", "expected"),
    [
        pytest.param(
            [ConnectionRefusedError(111, "refused"), ConnectionRefusedError(111, "refused")],
            "refused",
            id="dual-stack-refused",
        ),
        pytest.param(
            [socket.gaierror(-2, "Name or service not known")],
            "dns_failure",
            id="gaierror",
        ),
        pytest.param([ssl.SSLError("handshake failure")], "tls_error", id="sslerror"),
    ],
)
def test_group_children_are_classified_not_collapsed(
    children: list[Exception], expected: str
) -> None:
    """A ConnectError whose real causes sit in an ExceptionGroup gets the
    specific code (refused/dns_failure/tls_error), never 'unreachable'.

    ``tls=False`` proves the classification comes from the group
    children, not from the https TLS-phase hint.
    """
    assert _classify(_anyio_multi_connect_error(children), tls=False) == expected


@pytest.mark.parametrize(
    ("children", "expected"),
    [
        pytest.param(
            [ConnectionRefusedError(111, "refused"), ssl.SSLError("alert")],
            "tls_error",
            id="tls-beats-refused",
        ),
        pytest.param(
            [ConnectionRefusedError(111, "refused"), socket.gaierror(-2, "nxdomain")],
            "dns_failure",
            id="dns-beats-refused",
        ),
    ],
)
def test_priority_picks_most_actionable_when_children_disagree(
    children: list[Exception], expected: str
) -> None:
    """Children disagreeing → priority tls_error > dns_failure > refused
    > timeout > unreachable decides (the reporter's v6-refused +
    v4-TLS-error case)."""
    assert _classify(_anyio_multi_connect_error(children), tls=False) == expected


@pytest.mark.parametrize(
    "cert_error",
    [
        ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer"),
        ssl.SSLError("certificate verify failed"),
    ],
)
def test_untrusted_ca_classifies_tls_error(cert_error: ssl.SSLError) -> None:
    """AC4: an https endpoint behind a private CA the trust bundle does
    not know reads tls_error, whether the verify failure arrives as a
    dual-stack group child or a direct ``__cause__``."""
    assert _classify(_anyio_multi_connect_error([cert_error]), tls=True) == "tls_error"

    direct = httpx.ConnectError("verify failed")
    direct.__cause__ = cert_error
    assert _classify(direct, tls=True) == "tls_error"


@pytest.mark.parametrize("inner", [anyio.BrokenResourceError(), anyio.EndOfStream()])
def test_tls_phase_stream_failure_on_https_is_tls_error(inner: Exception) -> None:
    """AC2: a start_tls-phase failure (peer closed / sent an alert
    mid-handshake) reaches us as a ConnectError wrapping anyio
    BrokenResourceError / EndOfStream with no ssl.SSLError — on an https
    probe that is tls_error, not unreachable."""
    exc = httpx.ConnectError("")
    exc.__cause__ = inner
    assert _classify(exc, tls=True) == "tls_error"


def test_broken_resource_on_plain_http_stays_unreachable() -> None:
    """The scheme gate: httpcore's connect_tcp also maps
    BrokenResourceError, so on a plain-HTTP probe (no handshake) it must
    NOT read tls_error."""
    exc = httpx.ConnectError("")
    exc.__cause__ = anyio.BrokenResourceError()
    assert _classify(exc, tls=False) == "unreachable"


def test_plain_oserror_connect_failure_is_unreachable_not_tls() -> None:
    """A generic connect OSError (e.g. EHOSTUNREACH 'no route to host')
    on https stays unreachable — the TLS-phase logic must not sweep every
    unrecognised https failure into tls_error."""
    exc = httpx.ConnectError("")
    exc.__cause__ = OSError(113, "No route to host")
    assert _classify(exc, tls=True) == "unreachable"


def test_cause_cycle_terminates() -> None:
    """The id-based seen-set guards a ``__cause__`` cycle (no hang)."""
    outer = httpx.ConnectError("outer")
    inner = OSError("inner")
    outer.__cause__ = inner
    inner.__cause__ = outer
    assert _classify(outer, tls=False) == "unreachable"


def test_error_detail_is_innermost_first_bounded_and_shaped() -> None:
    """AC3: error_detail is innermost-first {type, message} evidence."""
    detail = _detail(
        _anyio_multi_connect_error([ConnectionRefusedError(111, "Connection refused")])
    )
    assert detail
    assert all(set(entry) == {"type", "message"} for entry in detail)
    # Leaf cause first (most actionable), outer httpx wrapper last.
    assert detail[0]["type"] == "ConnectionRefusedError"
    assert detail[-1]["type"] == "httpx.ConnectError"


def test_error_detail_caps_entries_and_truncates_messages() -> None:
    """Deep chains and long messages are bounded so a probe result never
    carries an unbounded exception dump."""
    current: BaseException = OSError("x" * 500)
    for _ in range(20):
        wrapper = OSError("wrap")
        wrapper.__cause__ = current
        current = wrapper
    detail = _detail(current)
    assert len(detail) <= net_http_probe_mod._MAX_ERROR_DETAIL_ENTRIES
    # Innermost (the long leaf message) is first and truncated.
    assert detail[0]["message"].endswith("...")
    assert len(detail[0]["message"]) <= net_http_probe_mod._MAX_ERROR_DETAIL_MESSAGE_CHARS + 3


def test_response_schema_declares_error_detail() -> None:
    """The evidence field is part of the documented result contract."""
    schema = net_http_probe_mod._NET_HTTP_PROBE_RESPONSE_SCHEMA
    assert "error_detail" in schema["properties"]
    assert "error_detail" in schema["required"]


# ---------------------------------------------------------------------------
# host_header — vhost-routed health probes by IP (#2896)
# ---------------------------------------------------------------------------


def test_host_header_is_an_optional_string_in_the_schema() -> None:
    """AC1: host_header is schema-accepted, optional, additive.

    Present as a string property, absent from ``required`` (so existing
    callers are unaffected), and the object stays closed
    (``additionalProperties: false``).
    """
    schema = net_http_probe_mod.NET_HTTP_PROBE_PARAMETER_SCHEMA
    assert schema["properties"]["host_header"]["type"] == "string"
    assert schema["required"] == ["url"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("host_header", "expected_sni"),
    [
        ("vcfa.corp.example", "vcfa.corp.example"),
        ("vcfa.corp.example:8443", "vcfa.corp.example"),
        # No numeric trailing port ⇒ passed through untouched.
        ("vcfa.corp.example:", "vcfa.corp.example:"),
    ],
)
def test_sni_from_host_header_strips_only_a_numeric_port(
    host_header: str, expected_sni: str
) -> None:
    """The TLS SNI name is the bare hostname; the Host: header keeps its port."""
    assert net_http_probe_mod._sni_from_host_header(host_header) == expected_sni


async def test_host_header_sends_host_and_sni_on_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: an https probe by IP with host_header dials the IP but sends the
    vhost as both the ``Host:`` header and the TLS SNI / cert-verify name.

    ``sni_hostname`` is what httpcore reads as ``server_hostname`` — the
    single value that drives BOTH the wire SNI extension and the
    certificate hostname check — so setting it is what keeps ``verify`` on
    against a cert pinned to the vhost rather than the dialed IP (the
    #2002/#2863 seam). Transport-verified via respx.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.9")
    async with respx.mock(base_url="https://203.0.113.9") as mock:
        route = mock.head("/health").respond(200, headers={"X-App": "vcfa"})
        result = await net_http_probe(
            _make_operator(),
            None,
            {"url": "https://203.0.113.9/health", "host_header": "vcfa.corp.example:8443"},
        )

    assert route.called
    req = route.calls[0].request
    # Host header = the vhost verbatim (port preserved).
    assert req.headers["host"] == "vcfa.corp.example:8443"
    # But the dialed address stays the IP the allowlist gated.
    assert req.url.host == "203.0.113.9"
    # SNI / cert-verification name = the bare vhost hostname (port stripped).
    assert req.extensions["sni_hostname"] == "vcfa.corp.example"
    assert result["reachable"] is True
    assert result["status"] == 200


async def test_host_header_omitted_dispatches_byte_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: without host_header, Host / SNI derive from the URL exactly as
    before — no forced Host header, no sni_hostname extension."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.9")
    async with respx.mock(base_url="https://203.0.113.9") as mock:
        route = mock.head("/x").respond(200)
        await net_http_probe(_make_operator(), None, {"url": "https://203.0.113.9/x"})

    req = route.calls[0].request
    assert req.headers["host"] == "203.0.113.9"
    assert "sni_hostname" not in req.extensions


async def test_host_header_does_not_widen_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """AC2: the allowlist gates the DIALED url host, never host_header.

    A non-allowlisted IP carrying an allowlisted-looking host_header is
    still refused as a dispatch error (#2784) and no socket opens — the
    header cannot smuggle a probe past the floor.
    """

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("no request may run when the dialed host is refused")

    monkeypatch.setattr(net_http_probe_mod.httpx.AsyncClient, "send", _boom)
    # 'localhost' is the only allowlisted token; the dialed 192.168.1.5 is not.
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "localhost")

    result = await _dispatch_probe({"url": "http://192.168.1.5/health", "host_header": "localhost"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_probe_refused"
    # The refusal names the DIALED IP, not the host_header value.
    assert result.extras["host"] == "192.168.1.5"


async def test_host_header_override_is_dropped_after_the_first_redirect_hop(
    monkeypatch: pytest.MonkeyPatch,
    _registered_http_probe_op: None,
) -> None:
    """AC3: the override rides hop 1 only; the redirect target keeps its own
    canonical host, and each hop is re-gated (127.0.0.1 stays allowlisted).

    Carrying a forced ``Host:`` across hops would probe the wrong virtual
    host, so hop 2 must fall back to the redirect target's own host.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    srv = await _start_http_server(
        {
            "/start": _Route(status=302, headers={"Location": "/final"}),
            "/final": _Route(status=200, body=b"arrived"),
        }
    )
    try:
        result = await _dispatch_probe(
            {
                "url": f"{srv.origin}/start",
                "method": "GET",
                "host_header": "vhost.example:1234",
            }
        )
    finally:
        srv.server.close()
        await srv.server.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    assert body["status"] == 200
    assert [hop["status"] for hop in body["redirect_chain"]] == [302]
    # Hop 1 carried the forced vhost Host: header.
    assert srv.request_hosts[0] == "vhost.example:1234"
    # Hop 2 (the redirect target) carried its own canonical host — override dropped.
    assert srv.request_hosts[1] == f"127.0.0.1:{srv.port}"
