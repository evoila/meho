# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed op ``net.ntp_check`` + its registrar.

The fifth ``net.*`` op (#2410, Initiative #2405 T5) on the T1 keystone
(#2406). It performs an ``ntpdate -q`` / ``sntp``-parity read: send one
mode-3 (client) NTPv4 packet to ``host:port`` over an **unprivileged**
client UDP socket, read the server's reply, and report reachability plus
the **clock offset and skew of that server against the backplane's own
clock** (RFC 5905 §8), the stratum, reference id, root delay/dispersion,
and the leap indicator.

Clock skew is a load-bearing cause of TLS-certificate-validity and
Kerberos/auth failures, so "is this appliance's clock sane from here" is
a real pre-flight / post-mortem read. This op answers it read-only: it
never sets a clock.

Why the standard library and no dependency: a mode-3 SNTP request is a
fixed 48-byte packet (RFC 5905 §7.3) — a first byte of ``0x23`` (leap 0,
version 4, mode 3 client) and a transmit timestamp — and the reply is
the same 48-byte header. ``struct`` builds and parses it; ``asyncio``'s
``loop.create_datagram_endpoint`` sends and receives it off the event
loop. No raw socket and no added pod capability: a client UDP socket
needs none.

This op inherits the three T1 foundations verbatim:

* **Probe allowlist** — :func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`
  screens the dialed ``host`` *before* any socket opens
  (``MEHO_NETDIAG_PROBE_ALLOWLIST`` empty ⇒ every probe refused).
* **Audit-visible host:port** — the return dict carries the literal
  ``host``/``port`` (a host:port is not a secret), so the durable audit
  row's ``raw_payload`` answers "who probed what".
* **Return-failures contract** — a refused, timed-out, DNS-failed, or
  kiss-o'-death (KoD) query is the **product**, not an error: the handler
  returns ``{"reachable": false, "reason": <code>, ...}`` with dispatch
  ``status="ok"``. It never raises a ``connector_*`` error for a query
  that did not answer.

``safety_level="safe"`` + ``requires_approval=False``: a read-only
single-shot query that sends one packet and mutates nothing, so the probe
allowlist is the sole floor (same posture as ``net.tcp_check``).
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import structlog

from meho_backplane.connectors.net.allowlist import (
    ProbeNotAllowedError,
    assert_probe_allowed,
)

# Reuse the shared probe timeout bounds/clamp from the keystone module.
# ``ops`` never imports ``ntp`` (the __init__ queues each registrar
# independently), so this package-internal import forms no cycle.
from meho_backplane.connectors.net.ops import (
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_TIMEOUT_SECONDS,
    _clamp_timeout,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_NTP_CHECK_PARAMETER_SCHEMA",
    "net_ntp_check",
    "register_net_ntp_check_operation",
]

_log = structlog.get_logger(__name__)

#: The well-known NTP service port; the caller rarely overrides it.
_DEFAULT_NTP_PORT: Final[int] = 123

#: Seconds between the NTP epoch (1900-01-01) and the Unix epoch
#: (1970-01-01) — RFC 5905 §6. Added to a Unix time to get NTP seconds.
_NTP_EPOCH_OFFSET: Final[int] = 2_208_988_800

#: 2**32, the divisor that turns an NTP timestamp fraction into a
#: sub-second float, and 2**16 for the 16.16 fixed-point "short" format
#: used by root delay / root dispersion (RFC 5905 §6).
_FRAC_32: Final[int] = 1 << 32
_FRAC_16: Final[int] = 1 << 16

#: First octet of a client request: leap indicator 0, version 4, mode 3
#: (client) → ``0b00_100_011`` = ``0x23`` (RFC 5905 §7.3).
_CLIENT_FIRST_OCTET: Final[int] = 0x23

#: The 48-byte NTP packet header (RFC 5905 §7.3): leap/version/mode,
#: stratum, poll (signed), precision (signed), then eleven 32-bit words —
#: root delay, root dispersion, reference id, and the four 64-bit
#: timestamps (reference / origin / receive / transmit) as sec+frac pairs.
_NTP_PACKET: Final[struct.Struct] = struct.Struct("!B B b b 11I")

NET_NTP_CHECK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Hostname or IP literal of the NTP server to query. Must "
                "be covered by MEHO_NETDIAG_PROBE_ALLOWLIST or the probe "
                "is refused before any socket opens."
            ),
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "UDP port the NTP service listens on (default 123).",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Query timeout in seconds (default 5, max 30). A query "
                "that does not answer in time returns reachable=false with "
                "reason='timeout'."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
}

_NET_NTP_CHECK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reachable": {
            "type": "boolean",
            "description": "True iff the server returned a usable time reply.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise a failure code: "
                "not_in_probe_allowlist, timeout, refused, dns_failure, "
                "unreachable, kod, invalid_response."
            ),
        },
        "host": {"type": "string", "description": "The queried host (audit-visible)."},
        "port": {"type": "integer", "description": "The queried UDP port (audit-visible)."},
        "stratum": {
            "type": ["integer", "null"],
            "description": (
                "Server stratum (1 = primary reference, 2-15 = secondary); null on any failure."
            ),
        },
        "ref_id": {
            "type": ["string", "null"],
            "description": (
                "Reference identifier: a 4-char refclock code at stratum 1 "
                "(e.g. 'GPS'), the upstream server's IPv4 at stratum >=2; "
                "null on failure."
            ),
        },
        "leap": {
            "type": ["integer", "null"],
            "description": (
                "Leap indicator (0 = in sync, 1 = +1s, 2 = -1s, 3 = "
                "unsynchronized/alarm); null on failure."
            ),
        },
        "offset_ms": {
            "type": ["number", "null"],
            "description": (
                "Server clock offset vs the backplane clock in milliseconds "
                "(RFC 5905): positive ⇒ the server is ahead. Null on failure."
            ),
        },
        "round_trip_ms": {
            "type": ["number", "null"],
            "description": "Measured round-trip delay in milliseconds; null on failure.",
        },
        "root_delay_ms": {
            "type": ["number", "null"],
            "description": "Server's total round-trip delay to the reference clock (ms).",
        },
        "root_dispersion_ms": {
            "type": ["number", "null"],
            "description": "Server's maximum error relative to the reference clock (ms).",
        },
        "kiss_code": {
            "type": ["string", "null"],
            "description": (
                "The 4-char kiss-o'-death code (e.g. 'RATE', 'DENY') when the "
                "server returned a stratum-0 KoD packet; null otherwise."
            ),
        },
    },
    "required": [
        "reachable",
        "reason",
        "host",
        "port",
        "stratum",
        "ref_id",
        "leap",
        "offset_ms",
        "round_trip_ms",
        "root_delay_ms",
        "root_dispersion_ms",
        "kiss_code",
    ],
    "additionalProperties": False,
}

_NET_NTP_CHECK_WHEN_TO_USE = (
    "Check an NTP server's clock offset and skew from the backplane's "
    "vantage — the 'ntpdate -q' / 'sntp' read: 'is this appliance's clock "
    "sane from here?', 'how far off is the time source?', 'what stratum is "
    "it?'. Clock skew is a common root cause of TLS-cert-validity and "
    "Kerberos/auth failures, so this is a real pre-flight / post-mortem "
    "probe. Read-only: it queries the time, it never sets a clock. A "
    "timeout, a refused query, or a kiss-o'-death reply is a normal "
    "result, not an error. The destination must be inside "
    "MEHO_NETDIAG_PROBE_ALLOWLIST."
)

_NET_NTP_CHECK_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to measure an NTP server's clock offset (skew) against the "
        "backplane clock and read its stratum before assuming a "
        "time-sync problem behind a TLS or Kerberos failure. Read-only: "
        "one client packet is sent, nothing is set."
    ),
    "parameter_hints": {
        "host": "Required. NTP server hostname or IP literal. Must be allowlisted for probing.",
        "port": "Optional. UDP port (default 123).",
        "timeout_seconds": "Optional. Query timeout (default 5, max 30).",
    },
    "output_shape": (
        "On success: {'reachable': true, 'reason': null, 'stratum': <int>, "
        "'ref_id': <str>, 'leap': <int>, 'offset_ms': <float>, "
        "'round_trip_ms': <float>, 'root_delay_ms': <float>, "
        "'root_dispersion_ms': <float>, 'kiss_code': null, 'host', 'port'}. "
        "On a refused / timed-out / KoD query: the same keys with "
        "reachable=false, reason set, and the clock fields null — still a "
        "successful (status=ok) op."
    ),
}


class _NtpReply(NamedTuple):
    """The parsed, RFC-5905-derived fields of one NTP server reply."""

    leap: int
    stratum: int
    ref_id: str
    kiss_code: str | None
    offset_ms: float
    round_trip_ms: float
    root_delay_ms: float
    root_dispersion_ms: float


def _encode_ntp_timestamp(unix_time: float) -> tuple[int, int]:
    """Split a Unix time into an NTP (seconds, fraction) 32-bit pair.

    The integer seconds are masked to 32 bits so a time past the 2036
    NTP era-0 rollover still packs; the fraction is the sub-second part
    scaled by 2**32 (RFC 5905 §6).
    """
    ntp = unix_time + _NTP_EPOCH_OFFSET
    seconds = int(ntp)
    fraction = int((ntp - seconds) * _FRAC_32)
    return seconds & 0xFFFFFFFF, fraction & 0xFFFFFFFF


def _decode_ntp_timestamp(seconds: int, fraction: int) -> float | None:
    """Turn an NTP (seconds, fraction) pair back into a Unix time.

    An all-zero timestamp means the field was never set (RFC 5905 uses it
    for "unknown"); it decodes to ``None`` so callers treat the reply as
    invalid rather than computing an offset against 1900.
    """
    if seconds == 0 and fraction == 0:
        return None
    return seconds + fraction / _FRAC_32 - _NTP_EPOCH_OFFSET


def _ntp_short_to_ms(value: int) -> float:
    """Convert an NTP 16.16 fixed-point "short" value to milliseconds."""
    return (value / _FRAC_16) * 1000.0


def _format_ref_id(ref_id_word: int, stratum: int) -> str:
    """Render the reference-id word per the stratum (RFC 5905 §7.3).

    At stratum 0 (KoD) or 1 the word is four ASCII characters naming the
    kiss code or the reference clock (e.g. ``GPS``); at stratum >=2 it is
    the IPv4 address of the upstream server, rendered dotted-decimal.
    """
    raw = struct.pack("!I", ref_id_word)
    if stratum <= 1:
        return raw.rstrip(b"\x00").decode("ascii", errors="replace")
    return ".".join(str(octet) for octet in raw)


def _build_request(transmit_unix: float) -> bytes:
    """Build the 48-byte mode-3 client request with its transmit timestamp.

    Only the first octet (client mode) and the transmit timestamp (the
    origin timestamp the server echoes back) are set; every other field
    is zero, which is what a client sends (RFC 5905 §7.3).
    """
    seconds, fraction = _encode_ntp_timestamp(transmit_unix)
    words = [0] * 11
    words[9] = seconds
    words[10] = fraction
    return _NTP_PACKET.pack(_CLIENT_FIRST_OCTET, 0, 0, 0, *words)


def _parse_reply(data: bytes, t1: float, t4: float, sent: tuple[int, int]) -> _NtpReply | None:
    """Parse a server reply and compute the offset/delay, or ``None``.

    ``t1`` is our transmit time and ``t4`` our receive time (both on the
    backplane clock); ``sent`` is the (seconds, fraction) we put in the
    request. Returns ``None`` when the reply is too short, does not echo
    our transmit timestamp in its origin field (an off-path / stale
    packet), or carries an unset server timestamp — all "invalid_response"
    cases. A stratum-0 KoD reply parses successfully with ``kiss_code``
    set and the clock fields zeroed; the caller maps it to a KoD failure.

    Offset and round-trip follow RFC 5905 §8 with T1/T4 on the backplane
    clock and T2/T3 (receive/transmit) on the server clock::

        offset     = ((T2 - T1) + (T3 - T4)) / 2
        round_trip = (T4 - T1) - (T3 - T2)
    """
    if len(data) < _NTP_PACKET.size:
        return None
    first, stratum, _poll, _precision, *words = _NTP_PACKET.unpack(data[: _NTP_PACKET.size])
    leap = (first >> 6) & 0x3
    root_delay, root_dispersion, ref_id_word = words[0], words[1], words[2]

    # Anti-spoof / anti-stale: the reply's origin timestamp must echo the
    # transmit timestamp we sent. A mismatch means this is not the answer
    # to our request.
    if (words[5], words[6]) != sent:
        return None

    kiss_code: str | None = None
    if stratum == 0:
        # Kiss-o'-death: the ref-id holds a 4-char code and no usable time.
        kiss_code = _format_ref_id(ref_id_word, stratum)
        return _NtpReply(leap, stratum, "", kiss_code, 0.0, 0.0, 0.0, 0.0)

    t2 = _decode_ntp_timestamp(words[7], words[8])  # server receive
    t3 = _decode_ntp_timestamp(words[9], words[10])  # server transmit
    if t2 is None or t3 is None:
        return None

    offset = ((t2 - t1) + (t3 - t4)) / 2
    round_trip = (t4 - t1) - (t3 - t2)
    return _NtpReply(
        leap=leap,
        stratum=stratum,
        ref_id=_format_ref_id(ref_id_word, stratum),
        kiss_code=None,
        offset_ms=offset * 1000.0,
        round_trip_ms=round_trip * 1000.0,
        root_delay_ms=_ntp_short_to_ms(root_delay),
        root_dispersion_ms=_ntp_short_to_ms(root_dispersion),
    )


class _NtpClientProtocol(asyncio.DatagramProtocol):
    """One-shot datagram protocol: send the request, capture the reply.

    ``connection_made`` stamps the transmit time as close to the send as
    possible (the ``t1`` used for the offset math) and fires the packet;
    ``datagram_received`` stamps ``t4`` and resolves the future. A socket
    error (an ICMP port-unreachable surfaces here on a connected UDP
    socket) rejects the future so the handler can map it to a reason.
    """

    def __init__(self, future: asyncio.Future[tuple[bytes, float]]) -> None:
        self._future = future
        self._transport: asyncio.DatagramTransport | None = None
        self.t1: float = 0.0
        self.sent: tuple[int, int] = (0, 0)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        self.t1 = time.time()
        self.sent = _encode_ntp_timestamp(self.t1)
        transport.sendto(_build_request(self.t1))  # type: ignore[attr-defined]

    def datagram_received(self, data: bytes, _addr: Any) -> None:
        if not self._future.done():
            self._future.set_result((data, time.time()))

    def error_received(self, exc: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and not self._future.done():
            self._future.set_exception(exc)


async def _query_ntp(
    host: str, port: int, timeout: float
) -> tuple[bytes, float, float, tuple[int, int]]:
    """Send one client packet to ``host:port`` and await the reply.

    Returns ``(reply_bytes, t1, t4, sent)``. ``create_datagram_endpoint``
    with ``remote_addr`` resolves DNS and connects the socket (so an
    unreachable/refused peer surfaces as an ``OSError``); the whole
    exchange is bounded by ``timeout``.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[bytes, float]] = loop.create_future()
    transport, protocol = await asyncio.wait_for(
        loop.create_datagram_endpoint(
            lambda: _NtpClientProtocol(future),
            remote_addr=(host, port),
        ),
        timeout=timeout,
    )
    try:
        data, t4 = await asyncio.wait_for(future, timeout=timeout)
    finally:
        transport.close()
    return data, protocol.t1, t4, protocol.sent


def _failure(host: str, port: int, reason: str, *, kiss_code: str | None = None) -> dict[str, Any]:
    """Build the uniform structured payload shared by every failed query."""
    return {
        "reachable": False,
        "reason": reason,
        "host": host,
        "port": port,
        "stratum": None,
        "ref_id": None,
        "leap": None,
        "offset_ms": None,
        "round_trip_ms": None,
        "root_delay_ms": None,
        "root_dispersion_ms": None,
        "kiss_code": kiss_code,
    }


def _connect_failure_reason(exc: BaseException) -> str:
    """Map a socket exception to a return-failures reason code.

    Order is significant: :class:`socket.gaierror`, :class:`TimeoutError`,
    and :class:`ConnectionRefusedError` are all :class:`OSError`
    subclasses checked before the generic ``OSError`` fallthrough. On a
    connected UDP socket an ICMP port-unreachable arrives as
    ``ConnectionRefusedError``.
    """
    if isinstance(exc, socket.gaierror):
        return "dns_failure"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    return "unreachable"


async def net_ntp_check(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Query an NTP server and report its clock offset/skew from the backplane.

    Op-id: ``net.ntp_check``. Synthetic typed op (no vendor connector,
    ``target`` is always ``None``). The dispatcher has validated the param
    schema, so ``host`` is present and well-typed.

    Flow: screen ``host`` against the probe allowlist → send one mode-3
    NTPv4 client packet over an unprivileged UDP socket under
    :func:`asyncio.wait_for` → parse the reply and compute offset/delay
    per RFC 5905 §8 against the backplane's own clock. A refused,
    timed-out, DNS-failed, kiss-o'-death, or malformed reply returns a
    structured ``reachable=false`` payload with ``status="ok"`` (the
    return-failures contract); it is never raised as a ``connector_*``
    error. The returned dict carries the literal ``host``/``port`` so the
    durable audit row's ``raw_payload`` records what was probed.
    """
    host = str(params["host"])
    port = int(params.get("port", _DEFAULT_NTP_PORT))
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.ntp_check.refused", host=host, port=port, reason="not_in_probe_allowlist")
        return _failure(host, port, "not_in_probe_allowlist")

    try:
        data, t1, t4, sent = await _query_ntp(host, port, timeout)
    except TimeoutError:
        return _failure(host, port, "timeout")
    except OSError as exc:
        return _failure(host, port, _connect_failure_reason(exc))

    reply = _parse_reply(data, t1, t4, sent)
    if reply is None:
        return _failure(host, port, "invalid_response")
    if reply.kiss_code is not None:
        _log.info("net.ntp_check.kod", host=host, port=port, kiss_code=reply.kiss_code)
        return _failure(host, port, "kod", kiss_code=reply.kiss_code)

    return {
        "reachable": True,
        "reason": None,
        "host": host,
        "port": port,
        "stratum": reply.stratum,
        "ref_id": reply.ref_id,
        "leap": reply.leap,
        "offset_ms": reply.offset_ms,
        "round_trip_ms": reply.round_trip_ms,
        "root_delay_ms": reply.root_delay_ms,
        "root_dispersion_ms": reply.root_dispersion_ms,
        "kiss_code": None,
    }


async def register_net_ntp_check_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.ntp_check`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (a sibling registrar to ``net.tcp_check``'s), run after
    the connector eager-import pass. Registered under the same synthetic
    natural key as the keystone
    (``product="net", version="1.x", impl_id="net-probe"``), so it shares
    the ``net-probe-1.x`` wire ``connector_id``. Idempotent. ``safe`` +
    ``requires_approval=False`` — the probe allowlist is the only floor.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.ntp_check",
        handler=net_ntp_check,
        group_key="probe",
        when_to_use=_NET_NTP_CHECK_WHEN_TO_USE,
        summary="Measure an NTP server's clock offset/skew and stratum from the backplane.",
        description=(
            "Sends one mode-3 (client) NTPv4 packet to a host:port over an "
            "unprivileged UDP socket and reports reachability plus the "
            "server clock's offset and skew against the backplane's own "
            "clock (RFC 5905), the stratum, reference id, root "
            "delay/dispersion, and the leap indicator — 'ntpdate -q' / "
            "'sntp' parity. Read-only: it reads the time, it never sets a "
            "clock, and it needs no raw socket or added pod capability. The "
            "destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST or the "
            "probe is refused before any socket opens. A refused, timed-out, "
            "DNS-failed, or kiss-o'-death query returns reachable=false with "
            "a reason code and status=ok — a failed probe is the product, "
            "never a connector error."
        ),
        parameter_schema=NET_NTP_CHECK_PARAMETER_SCHEMA,
        response_schema=_NET_NTP_CHECK_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "ntp", "time"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_NTP_CHECK_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
