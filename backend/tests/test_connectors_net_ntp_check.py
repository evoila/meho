# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``net.ntp_check`` — #2410 (Initiative #2405 T5).

``ntpdate -q`` parity on the T1 ``net.*`` mold:

* offset + stratum + ref-id against a test NTP responder, dispatched
  targetless on a fresh boot; the offset is computed vs the backplane's
  clock (sign correct — proven with a fixture whose clock is skewed both
  ways);
* the **return-failures contract**: a timeout, a malformed reply, an
  off-path (origin-mismatch) reply, or a kiss-o'-death packet return
  ``{reachable: false, reason}`` with dispatch ``status="ok"`` — never a
  ``connector_*`` error;
* the queried ``host`` + ``port`` land in the durable audit row's
  ``raw_payload``; the ``host`` is probe-allowlist-gated before any socket
  opens.

The NTP query runs against an **in-process UDP fixture server** (real
``asyncio`` datagram wire path, no outbound network): the handler dials
``127.0.0.1:<ephemeral port>`` and the fixture answers with a well-formed
NTPv4 reply that echoes the request's transmit timestamp and offsets its
own clock by a configurable skew.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import ntp as net_ntp
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.ntp import register_net_ntp_check_operation
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.ntp_check"

_NTP_PACKET = struct.Struct("!B B b b 11I")
_EPOCH = 2_208_988_800


def _encode(unix_time: float) -> tuple[int, int]:
    ntp = unix_time + _EPOCH
    seconds = int(ntp)
    fraction = int((ntp - seconds) * (1 << 32))
    return seconds & 0xFFFFFFFF, fraction & 0xFFFFFFFF


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
def stub_embedding_service() -> Any:
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_net_ops(stub_embedding_service: Any) -> AsyncIterator[None]:
    """Upsert the ``net.ntp_check`` descriptor row for dispatch-driving tests."""
    await register_net_ntp_check_operation(embedding_service=stub_embedding_service)
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


async def _dispatch_ntp(params: dict[str, Any]) -> OperationResult:
    """Dispatch ``net.ntp_check`` through the real targetless path."""
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
# In-process UDP fixture NTP server
# ---------------------------------------------------------------------------


class _NtpFixture:
    """Controls how the fixture NTP server answers a query.

    Mutate the attributes to steer the reply: ``skew`` (seconds the
    server clock leads the backplane by), ``stratum``, ``ref_id_word``,
    ``leap``, ``root_delay`` / ``root_dispersion`` (NTP 16.16 short
    format), and ``mode`` (``answer`` | ``no_response`` | ``short`` |
    ``origin_mismatch``).
    """

    def __init__(self) -> None:
        self.mode = "answer"
        self.skew = 0.0
        self.stratum = 2
        # 203.0.113.1 — a TEST-NET-3 address rendered dotted at stratum >= 2.
        self.ref_id_word = struct.unpack("!I", bytes([203, 0, 113, 1]))[0]
        self.leap = 0
        self.root_delay = 0x0001_0000  # 1.0 s -> 1000 ms
        self.root_dispersion = 0x0000_8000  # 0.5 s -> 500 ms

    def build_response(self, request: bytes) -> bytes | None:
        if self.mode == "no_response":
            return None
        if self.mode == "short":
            return b"\x00" * 10
        _, _, _, _, *words = _NTP_PACKET.unpack(request[:48])
        origin_sec, origin_frac = words[9], words[10]
        if self.mode == "origin_mismatch":
            origin_sec ^= 0xFFFF
        server_now = time.time() + self.skew
        recv = _encode(server_now)
        transmit = _encode(server_now)
        first = (self.leap << 6) | (4 << 3) | 4  # leap, version 4, mode 4 (server)
        out = [0] * 11
        out[0] = self.root_delay
        out[1] = self.root_dispersion
        out[2] = self.ref_id_word
        out[3], out[4] = _encode(server_now)  # reference timestamp
        out[5], out[6] = origin_sec, origin_frac  # origin echo
        out[7], out[8] = recv
        out[9], out[10] = transmit
        return _NTP_PACKET.pack(first, self.stratum, 0, -6, *out)


class _NtpFixtureProtocol(asyncio.DatagramProtocol):
    def __init__(self, controller: _NtpFixture) -> None:
        self._controller = controller

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: Any) -> None:
        response = self._controller.build_response(data)
        if response is not None:
            self._transport.sendto(response, addr)  # type: ignore[attr-defined]


@pytest.fixture
async def ntp_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[_NtpFixture, int]]:
    """Bind a fixture NTP server on 127.0.0.1 and yield (controller, port)."""
    controller = _NtpFixture()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _NtpFixtureProtocol(controller), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("socket").getsockname()[1]
    try:
        yield controller, port
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Offset / stratum against a skewed responder — sign correctness
# ---------------------------------------------------------------------------


async def test_offset_and_stratum_against_skewed_responder(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """A +1h-skewed server yields offset ~= +3_600_000 ms — targetless, fresh boot."""
    controller, port = ntp_fixture
    controller.skew = 3600.0
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port})

    assert result.status == "ok", result.error
    body = result.result
    assert body["reachable"] is True
    assert body["reason"] is None
    assert body["stratum"] == 2
    assert body["ref_id"] == "203.0.113.1"
    assert body["leap"] == 0
    # Server leads by ~3600 s; the round-trip on loopback is sub-millisecond.
    assert abs(body["offset_ms"] - 3_600_000) < 1_000
    assert 0.0 <= body["round_trip_ms"] < 1_000
    assert abs(body["root_delay_ms"] - 1000.0) < 1.0
    assert abs(body["root_dispersion_ms"] - 500.0) < 1.0


async def test_negative_offset_sign(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """A server whose clock trails yields a negative offset (sign correct)."""
    controller, port = ntp_fixture
    controller.skew = -3600.0
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port})

    assert result.status == "ok", result.error
    assert result.result["reachable"] is True
    assert abs(result.result["offset_ms"] - (-3_600_000)) < 1_000


async def test_stratum_one_ref_id_is_ascii_refclock(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """At stratum 1 the ref-id is the 4-char ASCII reference-clock code."""
    controller, port = ntp_fixture
    controller.stratum = 1
    controller.ref_id_word = struct.unpack("!I", b"GPS\x00")[0]
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port})

    assert result.status == "ok", result.error
    assert result.result["stratum"] == 1
    assert result.result["ref_id"] == "GPS"


async def test_default_port_is_123(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """Omitting ``port`` dials the NTP default — proven by pointing the default at the fixture."""
    _controller, port = ntp_fixture
    monkeypatch.setattr(net_ntp, "_DEFAULT_NTP_PORT", port)
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1"})

    assert result.status == "ok", result.error
    assert result.result["reachable"] is True
    assert result.result["port"] == port


# ---------------------------------------------------------------------------
# Return-failures contract — status=ok, never connector_*
# ---------------------------------------------------------------------------


async def test_timeout_returns_reachable_false(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """A silent server returns reachable=false, reason='timeout', status=ok."""
    controller, port = ntp_fixture
    controller.mode = "no_response"
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port, "timeout_seconds": 0.3})

    assert result.status == "ok", result.error
    assert result.extras.get("exception_class") is None
    assert result.result["reachable"] is False
    assert result.result["reason"] == "timeout"
    assert result.result["offset_ms"] is None
    assert result.result["stratum"] is None


async def test_kiss_of_death_returns_reason_kod(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """A stratum-0 KoD reply returns reachable=false, reason='kod', with the kiss code."""
    controller, port = ntp_fixture
    controller.stratum = 0
    controller.ref_id_word = struct.unpack("!I", b"RATE")[0]
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port})

    assert result.status == "ok", result.error
    assert result.result["reachable"] is False
    assert result.result["reason"] == "kod"
    assert result.result["kiss_code"] == "RATE"


async def test_short_reply_is_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """A truncated (< 48 byte) reply returns reason='invalid_response'."""
    controller, port = ntp_fixture
    controller.mode = "short"
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port, "timeout_seconds": 1})

    assert result.status == "ok", result.error
    assert result.result["reachable"] is False
    assert result.result["reason"] == "invalid_response"


async def test_origin_mismatch_is_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """A reply that does not echo our transmit timestamp is rejected (anti-spoof)."""
    controller, port = ntp_fixture
    controller.mode = "origin_mismatch"
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port, "timeout_seconds": 1})

    assert result.status == "ok", result.error
    assert result.result["reachable"] is False
    assert result.result["reason"] == "invalid_response"


# ---------------------------------------------------------------------------
# Allowlist gating — host screened before any socket
# ---------------------------------------------------------------------------


async def test_empty_allowlist_refuses_before_any_socket(
    monkeypatch: pytest.MonkeyPatch,
    _registered_net_ops: None,
) -> None:
    """Empty allowlist ⇒ structured refusal, no UDP socket ever opened."""

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("no query may run when the probe is refused")

    monkeypatch.setattr(net_ntp, "_query_ntp", _boom)

    result = await _dispatch_ntp({"host": "10.0.0.1"})

    assert result.status == "ok", result.error
    assert result.result == {
        "reachable": False,
        "reason": "not_in_probe_allowlist",
        "host": "10.0.0.1",
        "port": 123,
        "stratum": None,
        "ref_id": None,
        "leap": None,
        "offset_ms": None,
        "round_trip_ms": None,
        "root_delay_ms": None,
        "root_dispersion_ms": None,
        "kiss_code": None,
    }


# ---------------------------------------------------------------------------
# Audit row records host + port
# ---------------------------------------------------------------------------


async def test_audit_row_records_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
    ntp_fixture: tuple[_NtpFixture, int],
    _registered_net_ops: None,
) -> None:
    """The durable audit row's raw_payload carries the probed host/port."""
    _controller, port = ntp_fixture
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.0/8")

    result = await _dispatch_ntp({"host": "127.0.0.1", "port": port})
    assert result.status == "ok", result.error

    rows = await _fetch_audit_rows()
    ntp_rows = [row for row in rows if row.path == _OP_ID]
    assert len(ntp_rows) == 1
    raw = ntp_rows[0].raw_payload
    assert raw is not None
    assert raw["host"] == "127.0.0.1"
    assert raw["port"] == port


# ---------------------------------------------------------------------------
# Pure-function unit tests — packet build/parse, reason mapping
# ---------------------------------------------------------------------------


def test_build_request_is_a_48_byte_client_packet() -> None:
    """The request is a 48-byte mode-3 client packet carrying the transmit ts."""
    now = time.time()
    request = net_ntp._build_request(now)
    assert len(request) == 48
    assert request[0] == 0x23  # leap 0, version 4, mode 3 (client)
    _, _, _, _, *words = _NTP_PACKET.unpack(request)
    assert (words[9], words[10]) == net_ntp._encode_ntp_timestamp(now)


def test_ntp_timestamp_roundtrips() -> None:
    """Encoding then decoding a Unix time returns it within sub-ms precision."""
    now = 1_700_000_000.5
    seconds, fraction = net_ntp._encode_ntp_timestamp(now)
    assert net_ntp._decode_ntp_timestamp(seconds, fraction) == pytest.approx(now, abs=1e-3)


def test_zero_timestamp_decodes_to_none() -> None:
    """An all-zero NTP timestamp is 'unset', decoding to None."""
    assert net_ntp._decode_ntp_timestamp(0, 0) is None


def test_ntp_short_to_ms_is_fixed_point() -> None:
    """The 16.16 short format converts to milliseconds."""
    assert net_ntp._ntp_short_to_ms(0x0001_0000) == pytest.approx(1000.0)
    assert net_ntp._ntp_short_to_ms(0x0000_8000) == pytest.approx(500.0)


def test_format_ref_id_by_stratum() -> None:
    """Ref-id renders as ASCII at stratum <=1 and as IPv4 at stratum >=2."""
    gps = struct.unpack("!I", b"GPS\x00")[0]
    ipv4 = struct.unpack("!I", bytes([203, 0, 113, 1]))[0]
    assert net_ntp._format_ref_id(gps, 1) == "GPS"
    assert net_ntp._format_ref_id(ipv4, 2) == "203.0.113.1"


def test_connect_failure_reason_mapping() -> None:
    """Each socket error maps to its return-failures reason code."""
    assert net_ntp._connect_failure_reason(socket.gaierror()) == "dns_failure"
    assert net_ntp._connect_failure_reason(TimeoutError()) == "timeout"
    assert net_ntp._connect_failure_reason(ConnectionRefusedError()) == "refused"
    assert net_ntp._connect_failure_reason(OSError()) == "unreachable"
