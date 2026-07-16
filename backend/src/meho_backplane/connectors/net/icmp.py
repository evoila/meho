# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics ICMP cohort — ``net.ping`` / ``net.trace`` /
``net.path_mtu`` and their registrar (T6 of the ``net.*`` family, #2411).

These three ops complete local-tool parity (``ping`` / ``traceroute`` /
``tracepath``) so a local agent never drops to the shell for path
diagnosis. They ride the T1 keystone (#2406) foundations verbatim — the
probe allowlist, the audit-visible host, and the return-failures
contract — in the ``secret.*`` synthetic-connector mold (no ``Connector``
class, module-level handlers dispatched with ``target=None``).

The load-bearing property of this cohort is its **pod-security posture**
(resolved 2026-07-12, unprivileged + graceful-degrade). Reading ICMP
replies/errors normally needs ``CAP_NET_RAW``; this cohort deliberately
avoids granting it to the credential-holding backplane pod and uses only
unprivileged Linux mechanisms:

* **``net.trace`` + ``net.path_mtu`` — fully unprivileged via
  ``IP_RECVERR``.** A connected UDP socket with increasing ``IP_TTL``
  (trace) or ``IP_PMTUDISC_DO`` (path_mtu) reads the ICMP
  ``TimeExceeded`` / ``fragmentation-needed`` replies off the socket
  **error queue** (``recvmsg(MSG_ERRQUEUE)``) — no added pod capability,
  no sysctl, works on any cluster.
* **``net.ping`` (ICMP echo) — best-effort.** Uses an ``IPPROTO_ICMP``
  **datagram** socket (unprivileged when the cluster permits the
  ``net.ipv4.ping_group_range`` sysctl). Where the pod's GID is outside
  that range the socket cannot be created, so ``net.ping`` returns
  ``{"available": false, "reason": "icmp_echo_unprivileged_unavailable"}``
  and points the caller at ``net.tcp_check`` — it **degrades, never
  crashes**, and never forces a capability grant.

No ``CAP_NET_RAW`` is added to the main app; a privileged sidecar is the
documented escalation path only (not implemented here). The chart gains
an optional, default-off ``net.ipv4.ping_group_range`` sysctl knob for
operators who want unprivileged ping.

Linux-only, IPv4-only (v1): the mechanisms are Linux socket ABI and the
ABI constants below are not exposed by the stdlib ``socket`` module, so
they are pinned as module-level integers (values from
``linux/in.h`` / ``linux/errqueue.h`` / ``linux/icmp.h``). On a
non-Linux host or a kernel that rejects the socket options every op
returns its structured ``available=false`` / ``completed=false`` result
rather than raising.

``safety_level="safe"`` + ``requires_approval=False`` (same posture as
the rest of the cohort): read-only path probes, so the probe allowlist
is the sole floor.
"""

from __future__ import annotations

import asyncio
import errno
import os
import select
import socket
import struct
import time
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.net.allowlist import (
    ProbeNotAllowedError,
    assert_probe_allowed,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_PATH_MTU_PARAMETER_SCHEMA",
    "NET_PING_PARAMETER_SCHEMA",
    "NET_TRACE_PARAMETER_SCHEMA",
    "net_path_mtu",
    "net_ping",
    "net_trace",
    "register_net_icmp_operations",
]

_log = structlog.get_logger(__name__)

# --- Linux socket ABI constants (not exposed by stdlib ``socket``) ---------
# Values from linux/in.h, linux/errqueue.h, linux/icmp.h. Stable kernel
# ABI; pinned here so the module imports on any platform (the ops fail
# closed with a structured result on a kernel that does not honour them).
_IP_RECVERR: Final = 11
_IP_MTU_DISCOVER: Final = 10
_IP_MTU: Final = 14
_IP_PMTUDISC_DO: Final = 2
_SO_EE_ORIGIN_ICMP: Final = 2
_SO_EE_ORIGIN_ICMP6: Final = 3
_ICMP_ECHO: Final = 8
_ICMP_ECHOREPLY: Final = 0
_ICMP_DEST_UNREACH: Final = 3
_ICMP_TIME_EXCEEDED: Final = 11
#: ``struct sock_extended_err`` (16 bytes): ee_errno, ee_origin, ee_type,
#: ee_code, ee_pad, ee_info, ee_data.
_SOCK_EXTENDED_ERR: Final = struct.Struct("=IBBBBII")

# --- Per-op bounds ---------------------------------------------------------
_DEFAULT_TRACE_PORT: Final = 33434  # traceroute's default high UDP base port
_DEFAULT_MAX_HOPS: Final = 30
_MAX_MAX_HOPS: Final = 64
_DEFAULT_HOP_TIMEOUT: Final = 1.0
_MAX_HOP_TIMEOUT: Final = 5.0
_DEFAULT_PROBE_COUNT: Final = 3
_MAX_PROBE_COUNT: Final = 10
_DEFAULT_PING_TIMEOUT: Final = 1.0
_MAX_PING_TIMEOUT: Final = 5.0
#: Hard ceiling on total wall time for a single trace, independent of
#: ``max_hops * per_hop_timeout``, so a pathological request cannot pin a
#: worker thread open.
_TRACE_HARD_WALL_SECONDS: Final = 60.0
_ICMP_UNAVAILABLE_REASON: Final = "icmp_echo_unprivileged_unavailable"


def _clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    """Resolve an integer param to ``[lo, hi]``; ``default`` on bad input."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))


def _clamp_float(raw: Any, default: float, hi: float) -> float:
    """Resolve a positive float param to ``(0, hi]``; ``default`` on bad input."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, hi)


def _checksum(data: bytes) -> int:
    """RFC 1071 one's-complement internet checksum over *data*."""
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def _build_echo_request(ident: int, seq: int) -> bytes:
    """Build an ICMP echo-request datagram (kernel rewrites id/checksum)."""
    payload = b"meho-net-ping\x00\x00\x00"
    header = struct.pack("!BBHHH", _ICMP_ECHO, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    checksum = _checksum(header + payload)
    header = struct.pack("!BBHHH", _ICMP_ECHO, 0, checksum, ident & 0xFFFF, seq & 0xFFFF)
    return header + payload


def _parse_extended_err(cmsg: bytes) -> tuple[int, int, int, str | None]:
    """Parse ``sock_extended_err`` + offender sockaddr from cmsg data.

    Returns ``(ee_origin, ee_type, ee_code, offender_ip)``. ``offender_ip``
    is the address of the node that emitted the ICMP error (the router for
    a TimeExceeded, the destination for a port-unreachable), or ``None``
    when the origin is not an ICMP node or the offender sockaddr is absent.
    """
    if len(cmsg) < _SOCK_EXTENDED_ERR.size:
        return (0, 0, 0, None)
    _errno, origin, ee_type, ee_code, _pad, _info, _data = _SOCK_EXTENDED_ERR.unpack_from(cmsg, 0)
    offender_ip = _parse_offender_addr(cmsg[_SOCK_EXTENDED_ERR.size :], origin)
    return (origin, ee_type, ee_code, offender_ip)


def _parse_offender_addr(offender: bytes, origin: int) -> str | None:
    """Extract the offender IP from the ``SO_EE_OFFENDER`` sockaddr tail."""
    if origin == _SO_EE_ORIGIN_ICMP and len(offender) >= 8:
        # struct sockaddr_in: family(2) port(2) addr(4) ...
        return socket.inet_ntoa(offender[4:8])
    if origin == _SO_EE_ORIGIN_ICMP6 and len(offender) >= 24:
        # struct sockaddr_in6: family(2) port(2) flowinfo(4) addr(16) ...
        return socket.inet_ntop(socket.AF_INET6, offender[8:24])
    return None


def _drain_icmp_error(sock: socket.socket, deadline: float) -> tuple[int, int, str | None] | None:
    """Wait for one ICMP error on *sock*'s error queue until *deadline*.

    Returns ``(ee_type, ee_code, offender_ip)`` for the first ICMP-origin
    error read, or ``None`` if the deadline lapses with none queued. The
    socket is non-blocking; readiness is polled on ``POLLERR`` — a socket
    error queue does **not** wake ``select``'s exceptional set (that is
    TCP out-of-band data), so ``poll`` is used and ``POLLERR`` is reported
    in ``revents`` unconditionally.
    """
    poller = select.poll()
    poller.register(sock.fileno(), select.POLLERR)
    while True:
        remaining_ms = (deadline - time.monotonic()) * 1000.0
        if remaining_ms <= 0:
            return None
        if not poller.poll(remaining_ms):
            return None  # deadline lapsed with no error queued
        try:
            _data, ancdata, _flags, _addr = sock.recvmsg(512, 1024, socket.MSG_ERRQUEUE)
        except BlockingIOError:
            continue
        except OSError:
            return None
        for level, _ctype, cmsg in ancdata:
            if level in (socket.IPPROTO_IP, socket.IPPROTO_IPV6):
                origin, ee_type, ee_code, offender = _parse_extended_err(cmsg)
                if origin in (_SO_EE_ORIGIN_ICMP, _SO_EE_ORIGIN_ICMP6):
                    return (ee_type, ee_code, offender)
        # An error was dequeued but not ICMP-origin (e.g. a local error);
        # keep waiting for an ICMP hop within the remaining budget.


# ---------------------------------------------------------------------------
# net.ping — unprivileged ICMP-datagram echo, degrades where unavailable
# ---------------------------------------------------------------------------


def _blocking_ping(host: str, count: int, timeout: float) -> dict[str, Any]:
    """Send *count* ICMP echoes to *host*, returning the reachability report.

    Runs off the event loop. Opens an unprivileged ``IPPROTO_ICMP``
    datagram socket; a :class:`PermissionError` (the pod's GID is outside
    ``net.ipv4.ping_group_range``) or :class:`OSError` on creation is the
    capability-absent signal and yields ``available=false``. Individual
    unanswered echoes are silent losses, not errors.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (PermissionError, OSError):
        return _ping_unavailable(host)

    rtts: list[float] = []
    ident = os.getpid() & 0xFFFF
    try:
        sock.settimeout(timeout)
        for seq in range(count):
            rtt = _ping_one(sock, host, ident, seq, timeout)
            if rtt is not None:
                rtts.append(rtt)
    finally:
        sock.close()

    received = len(rtts)
    return {
        "available": True,
        "reachable": received > 0,
        "reason": None if received > 0 else "timeout",
        "packets_sent": count,
        "packets_received": received,
        "rtt_ms": _rtt_stats(rtts),
        "host": host,
    }


def _ping_one(sock: socket.socket, host: str, ident: int, seq: int, timeout: float) -> float | None:
    """Send one echo and wait for its reply; return RTT in ms or ``None``."""
    packet = _build_echo_request(ident, seq)
    deadline = time.monotonic() + timeout
    try:
        sent_at = time.perf_counter()
        sock.sendto(packet, (host, 0))
    except OSError:
        return None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            data, _addr = sock.recvfrom(1024)
        except (TimeoutError, OSError):
            return None
        if len(data) >= 8 and data[0] == _ICMP_ECHOREPLY:
            reply_seq = struct.unpack_from("!H", data, 6)[0]
            if reply_seq == (seq & 0xFFFF):
                return (time.perf_counter() - sent_at) * 1000.0
        # Not our reply (or an error type) — keep waiting within budget.


def _rtt_stats(rtts: list[float]) -> dict[str, float] | None:
    """Summarise per-echo RTTs into min/avg/max (ms), or ``None`` if empty."""
    if not rtts:
        return None
    return {
        "min": min(rtts),
        "avg": sum(rtts) / len(rtts),
        "max": max(rtts),
    }


def _ping_unavailable(host: str) -> dict[str, Any]:
    """Capability-absent ping result (unprivileged ICMP echo not permitted)."""
    return {
        "available": False,
        "reachable": False,
        "reason": _ICMP_UNAVAILABLE_REASON,
        "packets_sent": 0,
        "packets_received": 0,
        "rtt_ms": None,
        "host": host,
    }


# ---------------------------------------------------------------------------
# net.trace — unprivileged UDP + IP_RECVERR TTL walk
# ---------------------------------------------------------------------------


def _blocking_trace(host: str, port: int, max_hops: int, hop_timeout: float) -> dict[str, Any]:
    """Walk the path to *host* with increasing TTL, reading ICMP via errqueue.

    Runs off the event loop. One connected UDP socket per hop (a fresh
    socket avoids stale error-queue entries carrying between TTLs); each
    sends a datagram at ``IP_TTL = ttl`` and reads the ICMP TimeExceeded
    (router) or DestUnreachable (destination) off the error queue. A hop
    that answers nothing within ``hop_timeout`` is a silent ``*`` hop.
    """
    hops: list[dict[str, Any]] = []
    reached = False
    wall_deadline = time.monotonic() + min(max_hops * hop_timeout + 2.0, _TRACE_HARD_WALL_SECONDS)
    try:
        for ttl in range(1, max_hops + 1):
            if time.monotonic() >= wall_deadline:
                break
            address, arrived, rtt_ms = _trace_one_hop(host, port, ttl, hop_timeout)
            hops.append({"ttl": ttl, "address": address, "rtt_ms": rtt_ms})
            if arrived:
                reached = True
                break
    except OSError:
        # Socket setup rejected by the kernel (non-Linux / locked down):
        # degrade rather than crash.
        return _trace_unavailable(host, port)

    return {
        "completed": True,
        "reason": None,
        "reached": reached,
        "hops": hops,
        "host": host,
        "port": port,
    }


def _trace_one_hop(
    host: str, port: int, ttl: int, hop_timeout: float
) -> tuple[str | None, bool, float | None]:
    """Probe a single TTL; return ``(hop_address, arrived_at_dest, rtt_ms)``.

    Raises :class:`OSError` only if the socket / setsockopt themselves are
    rejected (the degrade signal); a silent hop is ``(None, False, None)``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, _IP_RECVERR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        sock.setblocking(False)
        sock.connect((host, port))
        started = time.perf_counter()
        try:
            sock.send(b"\x00" * 32)
        except OSError:
            return (None, False, None)
        result = _drain_icmp_error(sock, time.monotonic() + hop_timeout)
        if result is None:
            return (None, False, None)
        ee_type, _ee_code, offender = result
        rtt_ms = (time.perf_counter() - started) * 1000.0
        return (offender, _hop_is_destination(ee_type), rtt_ms)
    finally:
        sock.close()


def _hop_is_destination(ee_type: int) -> bool:
    """Whether an ICMP error marks arrival at the destination host.

    A ``DestUnreachable`` (type 3) means the probe reached the target host
    (port-unreachable is the expected code for a closed high UDP port, but
    host/proto-unreachable also means the packet got there). A
    ``TimeExceeded`` (type 11) is an intermediate router.
    """
    return ee_type == _ICMP_DEST_UNREACH


def _trace_unavailable(host: str, port: int) -> dict[str, Any]:
    """Degraded trace result when the kernel rejects the socket mechanism."""
    return {
        "completed": False,
        "reason": "trace_mechanism_unavailable",
        "reached": False,
        "hops": [],
        "host": host,
        "port": port,
    }


# ---------------------------------------------------------------------------
# net.path_mtu — unprivileged DF-probe PMTU discovery
# ---------------------------------------------------------------------------


def _blocking_path_mtu(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Discover the path MTU to *host* via DF probing + ``IP_MTU``.

    Runs off the event loop. Sets ``IP_PMTUDISC_DO`` (always set DF) and
    ``IP_RECVERR`` on a connected UDP socket, then sends a datagram at the
    first-hop MTU; an oversized segment fails with ``EMSGSIZE`` and the
    kernel's PMTU cache (updated from the ICMP fragmentation-needed error
    carrying the next-hop MTU) is re-read via ``getsockopt(IP_MTU)``.
    Bounded to a handful of shrink iterations.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError:
        return _path_mtu_unavailable(host, port)
    try:
        sock.setsockopt(socket.IPPROTO_IP, _IP_MTU_DISCOVER, _IP_PMTUDISC_DO)
        sock.setsockopt(socket.IPPROTO_IP, _IP_RECVERR, 1)
        sock.settimeout(timeout)
        sock.connect((host, port))
        mtu = sock.getsockopt(socket.IPPROTO_IP, _IP_MTU)
        deadline = time.monotonic() + timeout
        for _ in range(_MAX_MAX_HOPS):
            if time.monotonic() >= deadline:
                break
            payload_len = max(mtu - 28, 0)  # IPv4 (20) + UDP (8) headers
            try:
                sock.send(b"\x00" * payload_len)
                break  # fit at this MTU with DF set
            except OSError as exc:
                if exc.errno != errno.EMSGSIZE:
                    break
                _drain_icmp_error(sock, min(deadline, time.monotonic() + 0.2))
                updated = sock.getsockopt(socket.IPPROTO_IP, _IP_MTU)
                if updated <= 0 or updated >= mtu:
                    break
                mtu = updated
    except OSError:
        return _path_mtu_unavailable(host, port)
    finally:
        sock.close()

    return {
        "available": True,
        "mtu": mtu if mtu > 0 else None,
        "reason": None if mtu > 0 else "no_path_mtu",
        "host": host,
        "port": port,
    }


def _path_mtu_unavailable(host: str, port: int) -> dict[str, Any]:
    """Degraded path-MTU result when the kernel rejects the mechanism."""
    return {
        "available": False,
        "mtu": None,
        "reason": "path_mtu_mechanism_unavailable",
        "host": host,
        "port": port,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def net_ping(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Measure ICMP-echo reachability + RTT to ``host`` (op-id ``net.ping``).

    Screens ``host`` against the probe allowlist, then runs the
    unprivileged ICMP-datagram probe off the event loop. Where the pod's
    GID is outside ``net.ipv4.ping_group_range`` the socket cannot open
    and the op returns ``available=false`` with reason
    ``icmp_echo_unprivileged_unavailable`` (``status="ok"``) rather than
    crashing — a graceful degrade, not a ``connector_*`` error.
    """
    host = str(params["host"])
    count = _clamp_int(params.get("count"), _DEFAULT_PROBE_COUNT, 1, _MAX_PROBE_COUNT)
    timeout = _clamp_float(params.get("timeout_seconds"), _DEFAULT_PING_TIMEOUT, _MAX_PING_TIMEOUT)

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.ping.refused", host=host, reason="not_in_probe_allowlist")
        result = _ping_unavailable(host)
        result["reason"] = "not_in_probe_allowlist"
        return result

    return await asyncio.to_thread(_blocking_ping, host, count, timeout)


async def net_trace(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Trace the hop path to ``host`` (op-id ``net.trace``).

    Screens ``host`` against the probe allowlist, then walks the path with
    increasing TTL off the event loop, reading ICMP TimeExceeded /
    DestUnreachable via the unprivileged ``IP_RECVERR`` error queue — no
    pod capability required. Each hop reports ``address`` (``null`` for a
    silent ``*`` hop) and ``rtt_ms``. Fully unprivileged everywhere.
    """
    host = str(params["host"])
    port = _clamp_int(params.get("port"), _DEFAULT_TRACE_PORT, 1, 65535)
    max_hops = _clamp_int(params.get("max_hops"), _DEFAULT_MAX_HOPS, 1, _MAX_MAX_HOPS)
    hop_timeout = _clamp_float(
        params.get("hop_timeout_seconds"), _DEFAULT_HOP_TIMEOUT, _MAX_HOP_TIMEOUT
    )

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.trace.refused", host=host, reason="not_in_probe_allowlist")
        result = _trace_unavailable(host, port)
        result["reason"] = "not_in_probe_allowlist"
        return result

    return await asyncio.to_thread(_blocking_trace, host, port, max_hops, hop_timeout)


async def net_path_mtu(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Discover the largest unfragmented packet to ``host`` (``net.path_mtu``).

    Screens ``host`` against the probe allowlist, then runs the
    unprivileged DF-probe PMTU discovery off the event loop (``IP_RECVERR``
    + ``IP_PMTUDISC_DO``, next-hop MTU read from ``getsockopt(IP_MTU)``) —
    no pod capability required. Returns the discovered ``mtu`` in bytes.
    """
    host = str(params["host"])
    port = _clamp_int(params.get("port"), _DEFAULT_TRACE_PORT, 1, 65535)
    timeout = _clamp_float(params.get("timeout_seconds"), _DEFAULT_PING_TIMEOUT, _MAX_HOP_TIMEOUT)

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.path_mtu.refused", host=host, reason="not_in_probe_allowlist")
        result = _path_mtu_unavailable(host, port)
        result["reason"] = "not_in_probe_allowlist"
        return result

    return await asyncio.to_thread(_blocking_path_mtu, host, port, timeout)


# ---------------------------------------------------------------------------
# Parameter / response schemas
# ---------------------------------------------------------------------------

_HOST_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Hostname or IP literal to probe. Must be covered by "
        "MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is refused before any "
        "socket opens."
    ),
}

NET_PING_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_PROPERTY,
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_PROBE_COUNT,
            "description": (
                f"Echo requests to send (default {_DEFAULT_PROBE_COUNT}, max {_MAX_PROBE_COUNT})."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_PING_TIMEOUT,
            "description": (
                f"Per-echo reply timeout "
                f"(default {_DEFAULT_PING_TIMEOUT}, max {_MAX_PING_TIMEOUT})."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
}

NET_TRACE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_PROPERTY,
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": (
                f"UDP base port for the probes "
                f"(default {_DEFAULT_TRACE_PORT}, an unlikely-open high port)."
            ),
        },
        "max_hops": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_MAX_HOPS,
            "description": (
                f"Maximum TTL / hop count (default {_DEFAULT_MAX_HOPS}, max {_MAX_MAX_HOPS})."
            ),
        },
        "hop_timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_HOP_TIMEOUT,
            "description": (
                f"Per-hop reply timeout (default {_DEFAULT_HOP_TIMEOUT}, max {_MAX_HOP_TIMEOUT})."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
}

NET_PATH_MTU_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": _HOST_PROPERTY,
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": f"UDP port for the DF probes (default {_DEFAULT_TRACE_PORT}).",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_HOP_TIMEOUT,
            "description": (
                f"Overall discovery timeout "
                f"(default {_DEFAULT_PING_TIMEOUT}, max {_MAX_HOP_TIMEOUT})."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
}

_NET_PING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "available": {
            "type": "boolean",
            "description": (
                "False iff unprivileged ICMP echo is unavailable on this pod (degraded)."
            ),
        },
        "reachable": {"type": "boolean", "description": "True iff at least one echo was answered."},
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise not_in_probe_allowlist, timeout, "
                "or icmp_echo_unprivileged_unavailable."
            ),
        },
        "packets_sent": {"type": "integer", "description": "Echo requests sent."},
        "packets_received": {"type": "integer", "description": "Echo replies received."},
        "rtt_ms": {
            "type": ["object", "null"],
            "description": "min/avg/max RTT in ms across answered echoes; null when none answered.",
        },
        "host": {"type": "string", "description": "The probed host (audit-visible)."},
    },
    "required": [
        "available",
        "reachable",
        "reason",
        "packets_sent",
        "packets_received",
        "rtt_ms",
        "host",
    ],
    "additionalProperties": False,
}

_NET_TRACE_HOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ttl": {"type": "integer", "description": "The hop's TTL (1-based)."},
        "address": {
            "type": ["string", "null"],
            "description": "The responding hop's IP; null for a silent (*) hop.",
        },
        "rtt_ms": {
            "type": ["number", "null"],
            "description": "Round-trip time to the hop in ms; null for a silent hop.",
        },
    },
    "required": ["ttl", "address", "rtt_ms"],
    "additionalProperties": False,
}

_NET_TRACE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "completed": {
            "type": "boolean",
            "description": (
                "True iff the walk ran; false only when the kernel rejects the mechanism."
            ),
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on a completed walk; else not_in_probe_allowlist / "
                "trace_mechanism_unavailable."
            ),
        },
        "reached": {"type": "boolean", "description": "True iff a hop was the destination host."},
        "hops": {
            "type": "array",
            "items": _NET_TRACE_HOP_SCHEMA,
            "description": "Ordered hop list, TTL 1..N (empty when degraded).",
        },
        "host": {"type": "string", "description": "The traced host (audit-visible)."},
        "port": {"type": "integer", "description": "The UDP probe port (audit-visible)."},
    },
    "required": ["completed", "reason", "reached", "hops", "host", "port"],
    "additionalProperties": False,
}

_NET_PATH_MTU_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "available": {
            "type": "boolean",
            "description": "False iff the kernel rejects the DF-probe mechanism (degraded).",
        },
        "mtu": {
            "type": ["integer", "null"],
            "description": "Discovered path MTU in bytes; null when undiscoverable.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; else not_in_probe_allowlist / "
                "path_mtu_mechanism_unavailable / no_path_mtu."
            ),
        },
        "host": {"type": "string", "description": "The probed host (audit-visible)."},
        "port": {"type": "integer", "description": "The UDP probe port (audit-visible)."},
    },
    "required": ["available", "mtu", "reason", "host", "port"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------


async def register_net_icmp_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.ping`` / ``net.trace`` / ``net.path_mtu`` typed ops.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (a sibling registrar to the rest of the cohort), run
    after the connector eager-import pass. All three register under the
    shared synthetic natural key
    (``product="net", version="1.x", impl_id="net-probe"``) so they carry
    the ``net-probe-1.x`` wire ``connector_id``. Idempotent. ``safe`` +
    ``requires_approval=False`` — the probe allowlist is the only floor.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.ping",
        handler=net_ping,
        group_key="probe",
        when_to_use=(
            "Measure ICMP-echo reachability and round-trip time to a host — "
            "the 'ping' read: 'is the gateway up and how far away is it?'. "
            "Best-effort: where the cluster does not permit unprivileged "
            "ICMP echo the op returns available=false and you should use "
            "net.tcp_check instead. The destination must be inside "
            "MEHO_NETDIAG_PROBE_ALLOWLIST."
        ),
        summary=(
            "Ping a host (ICMP echo) for reachability and RTT; degrades "
            "where unprivileged echo is unavailable."
        ),
        description=(
            "Sends ICMP echo requests to a host over an unprivileged "
            "IPPROTO_ICMP datagram socket and reports reachability plus "
            "min/avg/max RTT. Where the pod's GID is outside "
            "net.ipv4.ping_group_range the socket cannot open and the op "
            "returns {available: false, reason: "
            "icmp_echo_unprivileged_unavailable} with status=ok — a graceful "
            "degrade pointing at net.tcp_check, never a crash. The "
            "destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST or the "
            "probe is refused before any socket opens."
        ),
        parameter_schema=NET_PING_PARAMETER_SCHEMA,
        response_schema=_NET_PING_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "icmp", "ping"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Use to confirm ICMP reachability and latency to a host. "
                "Best-effort: check the 'available' field — false means the "
                "cluster blocks unprivileged ICMP echo; fall back to "
                "net.tcp_check for a TCP-port reachability answer."
            ),
            "parameter_hints": {
                "host": "Required. Hostname or IP literal. Must be allowlisted.",
                "count": (
                    f"Optional. Echo count "
                    f"(default {_DEFAULT_PROBE_COUNT}, max {_MAX_PROBE_COUNT})."
                ),
                "timeout_seconds": (
                    f"Optional. Per-echo timeout "
                    f"(default {_DEFAULT_PING_TIMEOUT}, max {_MAX_PING_TIMEOUT})."
                ),
            },
            "output_shape": (
                "{'available': bool, 'reachable': bool, 'reason': <str|null>, "
                "'packets_sent': int, 'packets_received': int, 'rtt_ms': "
                "{min,avg,max}|null, 'host': str}. available=false ⇒ "
                "unprivileged echo unavailable; still status=ok."
            ),
        },
        embedding_service=embedding_service,
    )
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.trace",
        handler=net_trace,
        group_key="probe",
        when_to_use=(
            "Trace the network hop path to a host — the 'traceroute' read: "
            "'which routers does traffic to the appliance traverse and where "
            "does it stall?'. Fully unprivileged (no pod capability). Silent "
            "hops appear as null-address (*) entries. The destination must be "
            "inside MEHO_NETDIAG_PROBE_ALLOWLIST."
        ),
        summary="Trace the hop path to a host (traceroute) via unprivileged IP_RECVERR.",
        description=(
            "Walks the path to a host with increasing IP TTL over a connected "
            "UDP socket and reads the ICMP TimeExceeded (router) / "
            "DestUnreachable (destination) replies off the unprivileged "
            "IP_RECVERR error queue — no CAP_NET_RAW, works on any cluster. "
            "Returns an ordered hop list (per-hop address + RTT, null address "
            "for a silent * hop) and whether the destination was reached. A "
            "walk that hits no hops still returns status=ok. The destination "
            "must be inside MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is "
            "refused before any socket opens."
        ),
        parameter_schema=NET_TRACE_PARAMETER_SCHEMA,
        response_schema=_NET_TRACE_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "icmp", "traceroute"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Use to see the router path to a host and locate where "
                "connectivity stalls. Fully unprivileged. Silent hops are "
                "null-address entries, not errors."
            ),
            "parameter_hints": {
                "host": "Required. Hostname or IP literal. Must be allowlisted.",
                "port": f"Optional. UDP base port (default {_DEFAULT_TRACE_PORT}).",
                "max_hops": (
                    f"Optional. Max TTL (default {_DEFAULT_MAX_HOPS}, max {_MAX_MAX_HOPS})."
                ),
                "hop_timeout_seconds": (
                    f"Optional. Per-hop timeout "
                    f"(default {_DEFAULT_HOP_TIMEOUT}, max {_MAX_HOP_TIMEOUT})."
                ),
            },
            "output_shape": (
                "{'completed': bool, 'reason': <str|null>, 'reached': bool, "
                "'hops': [{'ttl': int, 'address': <str|null>, 'rtt_ms': "
                "<float|null>}], 'host': str, 'port': int}."
            ),
        },
        embedding_service=embedding_service,
    )
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.path_mtu",
        handler=net_path_mtu,
        group_key="probe",
        when_to_use=(
            "Discover the largest unfragmented packet size to a host — the "
            "'tracepath' read: 'is a black-hole MTU mismatch breaking large "
            "transfers to this endpoint?'. Fully unprivileged (no pod "
            "capability). The destination must be inside "
            "MEHO_NETDIAG_PROBE_ALLOWLIST."
        ),
        summary="Discover the path MTU to a host (tracepath) via unprivileged DF probing.",
        description=(
            "Discovers the path MTU to a host by sending DF-set datagrams "
            "over a connected UDP socket (IP_PMTUDISC_DO) and reading the "
            "next-hop MTU from the kernel PMTU cache (getsockopt IP_MTU), "
            "updated by the ICMP fragmentation-needed error off the "
            "unprivileged IP_RECVERR queue — no CAP_NET_RAW. Returns the "
            "discovered MTU in bytes. Undiscoverable / blocked returns "
            "status=ok with mtu=null. The destination must be inside "
            "MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is refused before any "
            "socket opens."
        ),
        parameter_schema=NET_PATH_MTU_PARAMETER_SCHEMA,
        response_schema=_NET_PATH_MTU_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "icmp", "mtu"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Use to diagnose MTU black-holes breaking large transfers to "
                "a host. Fully unprivileged. mtu=null means undiscoverable, "
                "not an error."
            ),
            "parameter_hints": {
                "host": "Required. Hostname or IP literal. Must be allowlisted.",
                "port": f"Optional. UDP probe port (default {_DEFAULT_TRACE_PORT}).",
                "timeout_seconds": (
                    f"Optional. Discovery timeout "
                    f"(default {_DEFAULT_PING_TIMEOUT}, max {_MAX_HOP_TIMEOUT})."
                ),
            },
            "output_shape": (
                "{'available': bool, 'mtu': <int|null>, 'reason': <str|null>, "
                "'host': str, 'port': int}."
            ),
        },
        embedding_service=embedding_service,
    )
