# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the net.* ICMP cohort — #2411 (Initiative #2405 T6).

Covers ``net.ping`` / ``net.trace`` / ``net.path_mtu``:

* all three register under the shared synthetic ``net-probe-1.x`` identity
  as ``safe`` + ungated typed ops;
* the probe allowlist gates every op **before** a socket opens (empty ⇒
  refused);
* the **degrade-not-crash** contract: an unprivileged-ICMP-absent ping
  (``PermissionError`` on socket creation) returns
  ``{available: false, reason: icmp_echo_unprivileged_unavailable}`` with
  dispatch ``status="ok"`` — never a ``connector_*`` error;
* the low-level Linux errqueue primitives (``sock_extended_err`` parse,
  ICMP echo build/checksum, destination detection) are exercised
  deterministically off synthetic bytes;
* a live loopback ``net.trace`` reaches ``127.0.0.1`` (skipped where the
  kernel does not honour ``IP_RECVERR``);
* the durable audit row records the literal host via ``raw_payload``;
* ``net.*`` ICMP ops classify as ``read`` in the broadcast taxonomy.

The autouse ``_default_database_url`` conftest fixture migrates the SQLite
DB to head so the descriptor / audit tables exist before the registrar runs.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select as sa_select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import icmp
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.icmp import register_net_icmp_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_icmp_ops(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    await register_net_icmp_operations(embedding_service=stub_embedding_service)
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


async def _dispatch(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=params,
    )


def _iprecverr_supported() -> bool:
    """Whether this kernel accepts the unprivileged IP_RECVERR UDP mechanism."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, icmp._IP_RECVERR, 1)
        finally:
            sock.close()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", ["net.ping", "net.trace", "net.path_mtu"])
async def test_icmp_ops_registered_as_safe_ungated(op_id: str, _registered_icmp_ops: None) -> None:
    """Each cohort op lands as a safe, ungated typed row under the net key."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            sa_select(EndpointDescriptor).where(
                EndpointDescriptor.product == "net",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "net-probe",
                EndpointDescriptor.op_id == op_id,
            )
        )
        row = result.scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False


def test_icmp_module_registers_no_connector_class() -> None:
    """The cohort module is synthetic — no register_connector(_v2) call."""
    source = Path(icmp.__file__).read_text()
    assert "register_connector_v2(" not in source
    assert "register_connector(" not in source


# ---------------------------------------------------------------------------
# Allowlist gate — refused before any socket opens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,params",
    [
        ("net.ping", {"host": "10.1.2.3"}),
        ("net.trace", {"host": "10.1.2.3"}),
        ("net.path_mtu", {"host": "10.1.2.3"}),
    ],
)
async def test_empty_allowlist_refuses_before_socket(
    op_id: str,
    params: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    _registered_icmp_ops: None,
) -> None:
    """Empty allowlist ⇒ structured refusal with no socket ever created."""

    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("socket must not open when the probe is refused")

    monkeypatch.setattr(icmp.socket, "socket", _boom)

    result = await _dispatch(op_id, params)
    assert result.status == "ok", result.error
    assert result.result["reason"] == "not_in_probe_allowlist"
    # No op crashed into a connector error.
    assert result.extras.get("exception_class") is None


# ---------------------------------------------------------------------------
# Degrade-not-crash — unprivileged ICMP echo unavailable
# ---------------------------------------------------------------------------


async def test_ping_degrades_when_icmp_socket_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
    _registered_icmp_ops: None,
) -> None:
    """A PermissionError creating the ICMP socket ⇒ available=false, status=ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")

    def _denied(*_a: object, **_kw: object) -> object:
        raise PermissionError("ping_group_range excludes this gid")

    monkeypatch.setattr(icmp.socket, "socket", _denied)

    result = await _dispatch("net.ping", {"host": "127.0.0.1"})
    assert result.status == "ok", result.error
    assert result.result == {
        "available": False,
        "reachable": False,
        "reason": "icmp_echo_unprivileged_unavailable",
        "packets_sent": 0,
        "packets_received": 0,
        "rtt_ms": None,
        "host": "127.0.0.1",
    }


async def test_trace_degrades_when_socket_rejected(
    monkeypatch: pytest.MonkeyPatch,
    _registered_icmp_ops: None,
) -> None:
    """A kernel that rejects the trace socket ⇒ completed=false, status=ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")

    def _denied(*_a: object, **_kw: object) -> object:
        raise OSError("socket type not supported")

    monkeypatch.setattr(icmp.socket, "socket", _denied)

    result = await _dispatch("net.trace", {"host": "127.0.0.1", "max_hops": 2})
    assert result.status == "ok", result.error
    assert result.result["completed"] is False
    assert result.result["reason"] == "trace_mechanism_unavailable"
    assert result.result["hops"] == []


# ---------------------------------------------------------------------------
# Low-level errqueue / ICMP primitives (synthetic, deterministic)
# ---------------------------------------------------------------------------


def test_build_echo_request_is_valid_icmp_echo() -> None:
    """The echo request has type 8 and a checksum that self-verifies to 0."""
    packet = icmp._build_echo_request(0x1234, 7)
    assert packet[0] == icmp._ICMP_ECHO
    # A correct one's-complement checksum makes the whole message sum to 0.
    assert icmp._checksum(packet) == 0
    assert struct.unpack_from("!H", packet, 6)[0] == 7  # seq survives


def _make_extended_err(ee_type: int, ee_code: int, offender_ip: str) -> bytes:
    """Build a synthetic ICMP-origin sock_extended_err + offender sockaddr."""
    body = struct.pack("=IBBBBII", 113, icmp._SO_EE_ORIGIN_ICMP, ee_type, ee_code, 0, 0, 0)
    sockaddr_in = (
        struct.pack("=H", socket.AF_INET)
        + b"\x00\x00"  # port
        + socket.inet_aton(offender_ip)
        + b"\x00" * 8  # sin_zero
    )
    return body + sockaddr_in


def test_parse_extended_err_reads_time_exceeded_router() -> None:
    """A TimeExceeded errqueue entry yields origin/type/code + offender IP."""
    cmsg = _make_extended_err(icmp._ICMP_TIME_EXCEEDED, 0, "192.0.2.1")
    origin, ee_type, ee_code, offender = icmp._parse_extended_err(cmsg)
    assert origin == icmp._SO_EE_ORIGIN_ICMP
    assert ee_type == icmp._ICMP_TIME_EXCEEDED
    assert ee_code == 0
    assert offender == "192.0.2.1"


def test_parse_extended_err_short_buffer_is_safe() -> None:
    """A truncated cmsg does not raise — it degrades to a null offender."""
    assert icmp._parse_extended_err(b"\x00\x00") == (0, 0, 0, None)


def test_hop_is_destination_distinguishes_dest_unreach_from_time_exceeded() -> None:
    assert icmp._hop_is_destination(icmp._ICMP_DEST_UNREACH) is True
    assert icmp._hop_is_destination(icmp._ICMP_TIME_EXCEEDED) is False


def test_rtt_stats_summarises_and_handles_empty() -> None:
    assert icmp._rtt_stats([]) is None
    assert icmp._rtt_stats([1.0, 3.0, 2.0]) == {"min": 1.0, "avg": 2.0, "max": 3.0}


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 30), ("bad", 30), (0, 1), (999, 64), (10, 10)],
)
def test_clamp_int_bounds(raw: object, expected: int) -> None:
    assert icmp._clamp_int(raw, 30, 1, 64) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 1.0), ("bad", 1.0), (-2.0, 1.0), (99.0, 5.0), (2.5, 2.5)],
)
def test_clamp_float_bounds(raw: object, expected: float) -> None:
    assert icmp._clamp_float(raw, 1.0, 5.0) == expected


# ---------------------------------------------------------------------------
# Live loopback trace + dispatch audit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _iprecverr_supported(),
    reason="kernel does not honour the unprivileged IP_RECVERR UDP mechanism",
)
async def test_trace_reaches_loopback(
    monkeypatch: pytest.MonkeyPatch,
    _registered_icmp_ops: None,
) -> None:
    """A real trace to loopback arrives at 127.0.0.1 on the first hop."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await _dispatch(
        "net.trace", {"host": "127.0.0.1", "max_hops": 3, "hop_timeout_seconds": 1.0}
    )
    assert result.status == "ok", result.error
    body = result.result
    assert body["completed"] is True
    assert body["reached"] is True
    assert body["hops"][-1]["address"] == "127.0.0.1"
    assert isinstance(body["hops"][-1]["rtt_ms"], float)


async def test_trace_audit_row_records_literal_host(
    monkeypatch: pytest.MonkeyPatch,
    _registered_icmp_ops: None,
) -> None:
    """The durable audit row's raw_payload carries the traced host:port.

    Degrade the mechanism so the test is kernel-independent — the audit
    contract (raw_payload = handler return, host-visible) holds regardless
    of whether a live hop was read.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.7")

    def _denied(*_a: object, **_kw: object) -> object:
        raise OSError("no route")

    monkeypatch.setattr(icmp.socket, "socket", _denied)

    result = await _dispatch("net.trace", {"host": "203.0.113.7", "port": 33434})
    assert result.status == "ok", result.error

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = list(
            (await session.execute(sa_select(AuditLog).where(AuditLog.path == "net.trace")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].raw_payload is not None
    assert rows[0].raw_payload["host"] == "203.0.113.7"
    assert rows[0].raw_payload["port"] == 33434


# ---------------------------------------------------------------------------
# Broadcast classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", ["net.ping", "net.trace", "net.path_mtu"])
def test_icmp_ops_classify_as_read(op_id: str) -> None:
    from meho_backplane.broadcast.events import classify_op

    assert classify_op(op_id) == "read"
