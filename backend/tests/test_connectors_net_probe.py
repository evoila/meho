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
    TENANT_PROBE_ALLOWLIST_ENV,
    ProbeNotAllowedError,
    assert_probe_allowed,
    parse_tenant_probe_allowlist,
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
_DEFAULT_TENANT_ID = UUID(int=0)


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
    # Default: no per-tenant bounds ⇒ every tenant inherits the instance
    # allowlist. Tests that need a per-tenant scope set it explicitly.
    monkeypatch.delenv(TENANT_PROBE_ALLOWLIST_ENV, raising=False)
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


def _make_operator(tenant_id: UUID = _DEFAULT_TENANT_ID) -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_check_as(params: dict[str, Any], *, tenant_id: UUID) -> OperationResult:
    """Dispatch ``net.tcp_check`` as an operator in *tenant_id*.

    Same targetless auto-exec path as :func:`_dispatch_check` — the op is
    ``safe`` + ``requires_approval=False`` so it runs with no approval
    prompt — but with a chosen tenant so the #3498 per-tenant bound is
    exercised on the real dispatch path (the bound catches an agent
    principal's auto-run exactly because it sits in the handler, after the
    permission verdict).
    """
    return await dispatch(
        operator=_make_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
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
    """Empty ``MEHO_NETDIAG_PROBE_ALLOWLIST`` ⇒ dispatch error, no socket.

    ``asyncio.open_connection`` is monkeypatched to fail the test if it
    is ever called — proving the refusal happens before the socket. #2784:
    the refusal is a ``connector_probe_refused`` **error**, never a
    reading-shaped ``connected=false`` payload a Sensor would read as a
    down host.
    """

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("open_connection must not run when the probe is refused")

    monkeypatch.setattr(net_ops.asyncio, "open_connection", _boom)

    result = await _dispatch_check({"host": "10.1.2.3", "port": 5432})

    assert result.status == "error"
    assert result.result is None
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["allowlist_env"] == PROBE_ALLOWLIST_ENV
    assert result.extras["host"] == "10.1.2.3"
    assert result.extras["exception_class"] == "ProbeNotAllowedError"
    # The operator-facing summary names the env var + the remediation, and
    # never echoes the destination (no internal-topology oracle).
    assert result.error is not None
    assert result.error.startswith("connector_probe_refused: ")
    assert PROBE_ALLOWLIST_ENV in result.error
    assert "netdiag.probeAllowlist" in result.error
    assert "10.1.2.3" not in result.error


async def test_host_outside_a_nonempty_allowlist_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """A non-empty allowlist still refuses a host it does not cover."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    result = await _dispatch_check({"host": "192.168.1.1", "port": 443})
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["host"] == "192.168.1.1"


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


# ---------------------------------------------------------------------------
# Per-tenant probe bound — MEHO_NETDIAG_PROBE_ALLOWLIST_TENANTS (#3498)
# ---------------------------------------------------------------------------

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")


def test_tenant_absent_from_map_inherits_instance_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant with no map entry falls through to the instance allowlist."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=192.168.0.0/16")

    # Tenant B has no entry → instance allowlist governs (10.0.0.0/8).
    assert_probe_allowed("10.9.9.9", tenant_id=_TENANT_B)
    with pytest.raises(ProbeNotAllowedError, match=PROBE_ALLOWLIST_ENV):
        assert_probe_allowed("192.168.1.1", tenant_id=_TENANT_B)


def test_tenant_scope_evaluated_before_instance_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded tenant's scope replaces the instance allowlist for it.

    Proves both the evaluation order (per-tenant first) and the
    replace-not-intersect semantics: a host in the instance allowlist but
    outside the tenant scope is refused, and a host in the tenant scope but
    outside the instance allowlist is allowed.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=192.168.5.0/24")

    # In the tenant scope but NOT in the instance allowlist → allowed.
    assert_probe_allowed("192.168.5.5", tenant_id=_TENANT_A)
    # In the instance allowlist but NOT in the tenant scope → refused, and
    # the refusal names the per-tenant knob, not the instance floor.
    with pytest.raises(ProbeNotAllowedError, match=TENANT_PROBE_ALLOWLIST_ENV):
        assert_probe_allowed("10.9.9.9", tenant_id=_TENANT_A)


def test_empty_tenant_scope_denies_every_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant present with an empty scope (``<uuid>=``) is denied everything.

    Even an address the instance allowlist would permit is refused.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=")

    with pytest.raises(ProbeNotAllowedError, match=TENANT_PROBE_ALLOWLIST_ENV):
        assert_probe_allowed("10.9.9.9", tenant_id=_TENANT_A)


def test_tenant_scope_matches_hostname_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-tenant scope honours the same verbatim-hostname grammar."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=db.internal, 172.16.0.0/12")

    assert_probe_allowed("db.internal", tenant_id=_TENANT_A)
    assert_probe_allowed("DB.Internal.", tenant_id=_TENANT_A)  # case + trailing dot
    assert_probe_allowed("172.16.1.1", tenant_id=_TENANT_A)
    with pytest.raises(ProbeNotAllowedError, match=TENANT_PROBE_ALLOWLIST_ENV):
        assert_probe_allowed("other.internal", tenant_id=_TENANT_A)


def test_tenant_key_casing_is_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upper-cased tenant key in the map still matches the operator UUID."""
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{str(_TENANT_A).upper()}=10.1.0.0/16")
    assert_probe_allowed("10.1.2.3", tenant_id=_TENANT_A)
    with pytest.raises(ProbeNotAllowedError, match=TENANT_PROBE_ALLOWLIST_ENV):
        assert_probe_allowed("10.2.2.3", tenant_id=_TENANT_A)


def test_no_tenant_id_uses_instance_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call with no ``tenant_id`` is never per-tenant-bounded (back-compat).

    Even with a populated map, a ``tenant_id=None`` call resolves against
    the instance allowlist exactly as before #3498.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "10.0.0.0/8")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=")
    assert_probe_allowed("10.9.9.9")  # no tenant_id → instance allowlist


def test_parse_tenant_probe_allowlist_rejects_bad_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, "not-a-uuid=10.0.0.0/8")
    with pytest.raises(ValueError, match=TENANT_PROBE_ALLOWLIST_ENV):
        parse_tenant_probe_allowlist()


def test_parse_tenant_probe_allowlist_rejects_missing_equals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}")
    with pytest.raises(ValueError, match=TENANT_PROBE_ALLOWLIST_ENV):
        parse_tenant_probe_allowlist()


def test_parse_tenant_probe_allowlist_rejects_bad_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=10.0.0.0/999")
    with pytest.raises(ValueError, match=TENANT_PROBE_ALLOWLIST_ENV):
        parse_tenant_probe_allowlist()


async def test_agent_in_bounded_tenant_cannot_probe_instance_only_address(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """The bound holds on the real auto-exec dispatch path (agent principal).

    A ``net.tcp_check`` from a tenant bounded to ``192.168.5.0/24`` against
    ``127.0.0.1`` — an address the instance allowlist permits but the tenant
    scope does not — fails the dispatch with ``connector_probe_refused`` and
    never opens a socket (``open_connection`` is stubbed to fail the test if
    reached). The op is ``safe`` + ``requires_approval=False``, so this IS
    the auto-exec path an agent principal takes: the bound sits in the
    handler, after the permission verdict, so no absent approval prompt can
    bypass it.
    """

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("open_connection must not run when the probe is refused")

    monkeypatch.setattr(net_ops.asyncio, "open_connection", _boom)
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=192.168.5.0/24")

    refused = await _dispatch_check_as({"host": "127.0.0.1", "port": 443}, tenant_id=_TENANT_A)
    assert refused.status == "error"
    assert refused.result is None
    assert refused.extras["error_code"] == "connector_probe_refused"
    assert refused.extras["host"] == "127.0.0.1"
    assert refused.extras["exception_class"] == "ProbeNotAllowedError"
    # The refusal names the per-tenant knob and stays address-free.
    assert refused.error is not None
    assert TENANT_PROBE_ALLOWLIST_ENV in refused.error
    assert "127.0.0.1" not in refused.error


async def test_unbounded_tenant_still_probes_instance_allowlisted_host(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_probe_op: None,
) -> None:
    """A tenant with no map entry keeps the instance allowlist's probe scope.

    Same populated per-tenant map as the refusal test, but a *different*
    tenant (absent from the map) probes an instance-allowlisted loopback
    host and connects — production sensors in unbounded tenants keep
    working while one tenant is bounded.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT_A}=192.168.5.0/24")

    server, port = await _serve_once()
    try:
        result = await _dispatch_check_as({"host": "127.0.0.1", "port": port}, tenant_id=_TENANT_B)
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "ok", result.error
    assert result.result["connected"] is True
    assert result.result["host"] == "127.0.0.1"
