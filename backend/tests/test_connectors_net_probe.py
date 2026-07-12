# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the net.* network-diagnostics connector — #2406 (Initiative #2405).

Covers the keystone mechanism this task establishes:

* ``net.tcp_check`` is a **synthetic** targetless typed op: it dispatches
  with ``target=None`` and no registered ``Target``, and the wire
  ``connector_id`` ``net-probe-1.x`` round-trips through the parser.
* The dedicated probe allowlist ``MEHO_NETDIAG_PROBE_ALLOWLIST`` has
  **inverted** semantics: empty ⇒ every probe refused *before a socket
  opens*; a host inside the allowlist connects.
* The **return-failures contract**: a refused / timed-out / DNS-failed
  connect returns ``{connected: false, reason}`` with dispatch
  ``status="ok"`` — never a ``connector_*`` error.
* The durable audit row records the literal ``host``/``port`` via
  ``raw_payload``.
* ``net.*`` ops classify as ``read`` in the broadcast taxonomy.
* No ``register_connector_v2`` for ``net`` (grep-pinned — it is synthetic).

The autouse ``_default_database_url`` conftest fixture migrates the
SQLite DB to head so the ``endpoint_descriptor`` / ``operation_group`` /
``audit_log`` tables exist before the registrar runs.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import ops as net_ops
from meho_backplane.connectors.net.allowlist import (
    PROBE_ALLOWLIST_ENV,
    ProbeNotAllowedError,
    assert_probe_allowed,
)
from meho_backplane.connectors.net.ops import net_tcp_check, register_net_typed_operations
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.tcp_check"


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the minimal Settings env + reset dispatcher caches per test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    # Default: allowlist unset ⇒ connector inert. Tests that need a
    # permitted host set MEHO_NETDIAG_PROBE_ALLOWLIST explicitly.
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
async def _registered_net_probe_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert the ``net.tcp_check`` descriptor row for dispatch-driving tests."""
    await register_net_typed_operations(embedding_service=stub_embedding_service)
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


async def _dispatch_check(params: dict[str, Any]) -> OperationResult:
    """Dispatch ``net.tcp_check`` through the real targetless path.

    ``target`` is ``None`` (synthetic product, no connector instance /
    registered target); the handler is module-level, so the dispatcher
    resolves it with ``connector_instance=None``. The op is
    ``requires_approval=False`` so no approval-resume flag is needed.
    """
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


async def _serve_once() -> tuple[asyncio.AbstractServer, int]:
    """Start a throwaway TCP server on 127.0.0.1 and return (server, port)."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# Synthetic identity + reachability + no-connector-class
# ---------------------------------------------------------------------------


def test_net_probe_connector_id_round_trips() -> None:
    """The wire connector_id resolves to the registered natural key.

    Guards the unreachable-identity trap (a non-digit-led version or a
    colon form would silently never match the descriptor).
    """
    assert parse_connector_id(_CONNECTOR_ID) == ("net", "1.x", "net-probe")


async def test_net_tcp_check_registered_as_safe_ungated_typed_op(
    _registered_net_probe_op: None,
) -> None:
    """The descriptor row carries the exact synthetic identity + posture."""
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


def test_net_connector_registers_no_connector_class() -> None:
    """``net`` is synthetic — no ``register_connector_v2`` anywhere under it."""
    net_pkg = Path(net_ops.__file__).parent
    sources = "\n".join(p.read_text() for p in net_pkg.glob("*.py"))
    # The invocation form (with the opening paren) — prose mentions of
    # the name in module docstrings are expected and must not trip this.
    assert "register_connector_v2(" not in sources
    assert "register_connector(" not in sources


async def test_net_tcp_check_connects_to_a_listening_port(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """Dispatch on a fresh boot, no registered target, host allowlisted → connects."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    server, port = await _serve_once()
    try:
        result = await _dispatch_check({"host": "127.0.0.1", "port": port})
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    assert body["connected"] is True
    assert body["reason"] is None
    assert isinstance(body["latency_ms"], float)
    assert body["host"] == "127.0.0.1"
    assert body["port"] == port


# ---------------------------------------------------------------------------
# Probe allowlist — empty = deny-all, refused before a socket opens
# ---------------------------------------------------------------------------


async def test_empty_allowlist_refuses_before_any_socket_opens(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """Empty ``MEHO_NETDIAG_PROBE_ALLOWLIST`` ⇒ structured refusal, no socket.

    ``asyncio.open_connection`` is monkeypatched to fail the test if it
    is ever called — proving the refusal happens before the socket.
    """

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("open_connection must not run when the probe is refused")

    monkeypatch.setattr(net_ops.asyncio, "open_connection", _boom)

    result = await _dispatch_check({"host": "10.1.2.3", "port": 5432})

    assert result.status == "ok", result.error
    assert result.result == {
        "connected": False,
        "reason": "not_in_probe_allowlist",
        "latency_ms": None,
        "host": "10.1.2.3",
        "port": 5432,
    }


async def test_host_outside_a_nonempty_allowlist_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """A non-empty allowlist still refuses a host it does not cover."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    result = await _dispatch_check({"host": "192.168.1.1", "port": 443})
    assert result.status == "ok", result.error
    assert result.result["connected"] is False
    assert result.result["reason"] == "not_in_probe_allowlist"


# ---------------------------------------------------------------------------
# Return-failures contract — a failed probe is status=ok, never connector_*
# ---------------------------------------------------------------------------


async def test_refused_connect_is_ok_status_not_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """A refused connect (closed port) returns connected=false, status=ok.

    Picks a closed port on loopback (allowlisted) so the OS refuses the
    connection deterministically.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    # Bind then immediately release a port so it is (almost certainly)
    # closed when we probe it a moment later.
    server, port = await _serve_once()
    server.close()
    await server.wait_closed()

    result = await _dispatch_check({"host": "127.0.0.1", "port": port})

    assert result.status == "ok", result.error
    assert result.extras.get("exception_class") is None
    assert result.result["connected"] is False
    assert result.result["reason"] == "refused"
    assert result.result["latency_ms"] is None


@pytest.mark.parametrize(
    "exc,expected_reason",
    [
        # asyncio.wait_for raises builtin TimeoutError (== asyncio.TimeoutError).
        (TimeoutError(), "timeout"),
        (socket.gaierror("name resolution failed"), "dns_failure"),
        (ConnectionRefusedError(), "refused"),
        (OSError("network is unreachable"), "unreachable"),
    ],
    ids=["timeout", "gaierror", "refused", "other-oserror"],
)
async def test_handler_maps_connect_exceptions_to_reason_codes(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_reason: str,
) -> None:
    """Every connect exception maps to a reason code, never re-raises.

    Handler-level (direct call) so each exception class is exercised
    deterministically without depending on real network conditions.
    ``TimeoutError`` subclasses ``OSError``, so this pins that the
    timeout arm is matched before the generic ``OSError`` arm.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.5")

    async def _raise(*_a: object, **_kw: object) -> object:
        raise exc

    monkeypatch.setattr(net_ops.asyncio, "open_connection", _raise)

    result = await net_tcp_check(_make_operator(), None, {"host": "203.0.113.5", "port": 9999})
    assert result == {
        "connected": False,
        "reason": expected_reason,
        "latency_ms": None,
        "host": "203.0.113.5",
        "port": 9999,
    }


# ---------------------------------------------------------------------------
# Audit row records the literal host:port
# ---------------------------------------------------------------------------


async def test_audit_row_records_literal_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """The durable audit row's raw_payload carries the probed host:port.

    The dispatcher stores the handler's return dict as ``raw_payload``;
    that dict carries host/port so the row answers 'who probed what'
    (params themselves are only hashed into ``payload``).
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    server, port = await _serve_once()
    try:
        result = await _dispatch_check({"host": "127.0.0.1", "port": port})
    finally:
        server.close()
        await server.wait_closed()
    assert result.status == "ok", result.error

    rows = await _fetch_audit_rows()
    probe_rows = [r for r in rows if r.path == _OP_ID]
    assert len(probe_rows) == 1
    raw = probe_rows[0].raw_payload
    assert raw is not None
    assert raw["host"] == "127.0.0.1"
    assert raw["port"] == port
    # The literal port survives into the durable record.
    assert str(port) in json.dumps(raw)


# ---------------------------------------------------------------------------
# Allowlist unit behaviour
# ---------------------------------------------------------------------------


def test_assert_probe_allowed_empty_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    with pytest.raises(ProbeNotAllowedError):
        assert_probe_allowed("127.0.0.1")


def test_assert_probe_allowed_ip_literal_in_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    assert_probe_allowed("10.9.9.9")  # in range → no raise
    with pytest.raises(ProbeNotAllowedError):
        assert_probe_allowed("11.0.0.1")


def test_assert_probe_allowed_hostname_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "db.internal, 10.0.0.0/8")
    assert_probe_allowed("db.internal")
    assert_probe_allowed("DB.Internal.")  # case-insensitive, trailing dot stripped
    with pytest.raises(ProbeNotAllowedError):
        assert_probe_allowed("other.internal")


def test_parse_probe_allowlist_rejects_malformed_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/999")
    with pytest.raises(ValueError, match=PROBE_ALLOWLIST_ENV):
        assert_probe_allowed("10.0.0.1")


# ---------------------------------------------------------------------------
# Broadcast classification — net.* is a read
# ---------------------------------------------------------------------------


def test_net_ops_classify_as_read() -> None:
    from meho_backplane.broadcast.events import classify_op

    assert classify_op("net.tcp_check") == "read"
    # Forward cover for the T2-T4 verbs that reuse this scaffolding.
    assert classify_op("net.dns_lookup") == "read"
    assert classify_op("net.http_probe") == "read"
