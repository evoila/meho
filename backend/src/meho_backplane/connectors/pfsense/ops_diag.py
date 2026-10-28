# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense diagnostics -- ``pfsense.diag.ping``.

One read op: a short ping that runs ON the firewall, from one of the
firewall's own addresses. A static route that ``pfsense.route.static.add``
wrote is only config. This ping shows that traffic really passes: with
the firewall's VPN-side address as the source, the packets take the same
path a VPN user's packets take.

Bounded on purpose
------------------

* ``host`` must be an IP address or a DNS host name. ``source`` must be an
  IP address. Both are checked before any SSH call, and both are quoted
  with :func:`shlex.quote` on top. No free text reaches the shell.
* ``source`` must be an address that ``ifconfig -a`` shows on the
  firewall. The op reads ``ifconfig -a`` first and refuses any other
  address.
* ``count`` is 1 to 5 (default 3). Each reply is awaited at most one
  second (``-W 1000``) and ping stops after ``count + 2`` seconds
  (``-t``), so the op ends within a few seconds.

The op changes nothing on the firewall, so it is ``safe`` and needs no
approval.

FreeBSD ``ping(8)`` (pfSense 2.7 runs FreeBSD 14) takes ``-4`` / ``-6``,
``-c``, ``-n``, ``-S``, ``-t`` and ``-W`` for both IPv4 and IPv6. Exit 0
means at least one reply, 2 means no reply; both print the summary lines
this module parses. Any other exit is an error (for example an unknown
host name).
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.pfsense.ops import PfSenseOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "DIAG_OPS",
    "firewall_addresses",
    "parse_ping_summary",
    "pfsense_diag_ping",
]

_DEFAULT_COUNT = 3
_MAX_COUNT = 5
_REPLY_WAIT_MS = 1000

# A DNS host name: dot-separated labels of letters, digits and dashes, no
# label starting or ending with a dash, at most 253 characters in total.
# The first character is a letter or digit, so a value can never look like
# a ping option.
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?"
)

# FreeBSD ping summary, for example:
#   3 packets transmitted, 3 packets received, 0.0% packet loss
#   round-trip min/avg/max/stddev = 0.412/0.501/0.633/0.094 ms
# The IPv6 form names the last field ``std-dev``; a reply count can be
# followed by ``+N duplicates,`` before the loss.
_TX_RE = re.compile(
    r"(?P<sent>\d+) packets transmitted, (?P<received>\d+) packets received,"
    r".*?(?P<loss>\d+(?:\.\d+)?)% packet loss"
)
# Any ``inet`` / ``inet6`` line of ``ifconfig -a``. Unlike the
# ``pfsense.interface.list`` parser this also takes the point-to-point form
# (``inet 198.51.100.1 --> 198.51.100.2 netmask ...``) that OpenVPN
# interfaces use, because the VPN-side address is the usual source here.
_IFCONFIG_ADDR_RE = re.compile(r"^\s+inet6?\s+(?P<addr>[0-9A-Fa-f.:]+)(?:%\S+)?(?:/\d+)?\s", re.M)

_RTT_RE = re.compile(
    r"round-trip min/avg/max/\S+ = (?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/"
)


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------


def _check_ip(value: str, field: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return *value* as an IP address, ``None`` when it is not one.

    A multicast, unspecified or scoped (``%zone``) address is refused.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if addr.is_multicast or addr.is_unspecified or "%" in value:
        raise ValueError(f"{field} must be a unicast address; got {value!r}")
    return addr


def _validate_host(value: Any) -> tuple[str, int | None]:
    """Return ``(host, ip_version)``; ``ip_version`` is ``None`` for a name."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"host must be an IP address or a host name; got {value!r}")
    host = value.strip()
    addr = _check_ip(host, "host")
    if addr is not None:
        return str(addr), addr.version
    if not _HOSTNAME_RE.fullmatch(host):
        raise ValueError(f"host must be an IP address or a host name; got {value!r}")
    return host, None


def _validate_source(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the source as an IP address; a name or other text is refused."""
    if not isinstance(value, str):
        raise ValueError(f"source must be an IP address of the firewall; got {value!r}")
    addr = _check_ip(value.strip(), "source")
    if addr is None:
        raise ValueError(f"source must be an IP address of the firewall; got {value!r}")
    return addr


def _validate_count(value: Any) -> int:
    if value is None:
        return _DEFAULT_COUNT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_COUNT:
        raise ValueError(f"count must be a whole number from 1 to {_MAX_COUNT}; got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def firewall_addresses(ifconfig_output: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every IP address that ``ifconfig -a`` shows on the firewall.

    >>> out = (
    ...     "ovpns1: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> metric 0 mtu 1500\\n"
    ...     "\\tinet 198.51.100.1 --> 198.51.100.2 netmask 0xffffff00\\n"
    ...     "\\tinet6 fe80::1%ovpns1 prefixlen 64 scopeid 0x5\\n"
    ... )
    >>> sorted(str(a) for a in firewall_addresses(out))
    ['198.51.100.1', 'fe80::1']
    """
    found: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for match in _IFCONFIG_ADDR_RE.finditer(ifconfig_output):
        try:
            found.add(ipaddress.ip_address(match.group("addr")))
        except ValueError:
            continue
    return found


def parse_ping_summary(output: str) -> dict[str, Any] | None:
    """Parse the FreeBSD ping summary; ``None`` when there is none.

    >>> out = (
    ...     "--- 192.0.2.10 ping statistics ---\\n"
    ...     "3 packets transmitted, 2 packets received, 33.3% packet loss\\n"
    ...     "round-trip min/avg/max/stddev = 0.412/0.501/0.633/0.094 ms\\n"
    ... )
    >>> parse_ping_summary(out)["packet_loss_percent"]
    33.3
    """
    tx = _TX_RE.search(output)
    if tx is None:
        return None
    rtt = _RTT_RE.search(output)
    return {
        "packets_sent": int(tx.group("sent")),
        "packets_received": int(tx.group("received")),
        "packet_loss_percent": float(tx.group("loss")),
        "rtt_ms": (
            {key: float(rtt.group(key)) for key in ("min", "avg", "max")}
            if rtt is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _text(proc: Any, attr: str) -> str:
    value = getattr(proc, attr, "") or ""
    return value if isinstance(value, str) else ""


async def pfsense_diag_ping(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.diag.ping`` -- a short ping from the firewall.

    Sequence: check the inputs -> read ``ifconfig -a`` and refuse a
    ``source`` that is not one of the firewall's addresses -> run
    ``ping -4|-6 -n -c <count> -W 1000 -t <count+2> -S <source> <host>``
    -> parse the summary.

    Returns ``{host, source, count, packets_sent, packets_received,
    packet_loss_percent, reachable, rtt_ms}``. Raises :exc:`ValueError` on
    bad input and :exc:`RuntimeError` when ``ifconfig`` or ``ping`` fails
    without a summary (for example an unknown host name).
    """
    host, host_version = _validate_host(params.get("host"))
    source = _validate_source(params.get("source"))
    count = _validate_count(params.get("count"))
    if host_version is not None and host_version != source.version:
        raise ValueError(
            f"host {host} and source {source} are not the same IP version; "
            "use an IPv4 source for an IPv4 host and an IPv6 source for an IPv6 host"
        )

    ifc = await self._run_command(target, "ifconfig -a", operator=operator)
    ifc_out = _text(ifc, "stdout")
    if ifc.exit_status != 0 and not ifc_out.strip():
        raise RuntimeError(f"ifconfig -a exit {ifc.exit_status}")
    if source not in firewall_addresses(ifc_out):
        raise ValueError(
            f"source {source} is not an address of this firewall; "
            "pick one from pfsense.interface.list"
        )

    cmd = (
        f"ping -{source.version} -n -c {count} -W {_REPLY_WAIT_MS} -t {count + 2} "
        f"-S {shlex.quote(str(source))} {shlex.quote(host)}"
    )
    proc = await self._run_command(target, cmd, operator=operator, timeout=float(count + 15))
    summary = parse_ping_summary(_text(proc, "stdout"))
    if summary is None:
        detail = (_text(proc, "stderr").strip().splitlines() or [""])[0][:200]
        message = f"ping did not run: exit {proc.exit_status}"
        raise RuntimeError(f"{message}: {detail}" if detail else message)
    return {
        "host": host,
        "source": str(source),
        "count": count,
        **summary,
        "reachable": summary["packets_received"] > 0,
    }


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_PING_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "maxLength": 253,
            "description": (
                "The address to ping: an IPv4 or IPv6 address, or a DNS host "
                "name, for example an address behind a new static route."
            ),
        },
        "source": {
            "type": "string",
            "minLength": 1,
            "maxLength": 45,
            "description": (
                "The source IP address. It must be one of the firewall's own "
                "addresses (see pfsense.interface.list), for example its "
                "VPN-side address. Same IP version as the host."
            ),
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_COUNT,
            "default": _DEFAULT_COUNT,
            "description": f"How many pings to send, 1 to {_MAX_COUNT}. Default {_DEFAULT_COUNT}.",
        },
    },
    "required": ["host", "source"],
    "additionalProperties": False,
    "examples": [
        {"host": "192.0.2.10", "source": "198.51.100.1"},
        {"host": "2001:db8::10", "source": "2001:db8:1::1", "count": 1},
    ],
}

_PING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {"type": "string"},
        "source": {"type": "string"},
        "count": {"type": "integer"},
        "packets_sent": {"type": "integer"},
        "packets_received": {"type": "integer"},
        "packet_loss_percent": {"type": "number"},
        "reachable": {"type": "boolean"},
        "rtt_ms": {
            "type": ["object", "null"],
            "properties": {
                "min": {"type": "number"},
                "avg": {"type": "number"},
                "max": {"type": "number"},
            },
        },
    },
    "required": [
        "host",
        "source",
        "count",
        "packets_sent",
        "packets_received",
        "packet_loss_percent",
        "reachable",
        "rtt_ms",
    ],
    "additionalProperties": False,
}

_PING_WHEN_TO_CALL = (
    "Call to prove that traffic passes, not only that config exists: for "
    "example after pfsense.route.static.add, ping an address behind the "
    "new route with the firewall's VPN-side address as the source. That "
    "is the path a VPN user takes. It sends a few pings and changes "
    "nothing on the firewall."
)

_PING_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": _PING_WHEN_TO_CALL,
    "when_to_use": _PING_WHEN_TO_CALL,
    "parameter_hints": {
        "host": "Required. IP address or DNS host name to ping.",
        "source": (
            "Required. One of the firewall's own IP addresses; get them from "
            "pfsense.interface.list. Other addresses are refused."
        ),
        "count": f"Optional. 1 to {_MAX_COUNT} pings (default {_DEFAULT_COUNT}).",
    },
    "output_shape": (
        "{host, source, count, packets_sent, packets_received, "
        "packet_loss_percent, reachable, rtt_ms: {min, avg, max} | null}. "
        "reachable is true when at least one reply came back. rtt_ms is null "
        "when no reply came back."
    ),
    "next_step": (
        "If reachable is false, check the route with pfsense.route.static.list, "
        "the gateway with pfsense.gateway.list, and the firewall rules with "
        "pfsense.firewall.rules. Some hosts never answer ping; a loss of 100% "
        "alone does not prove the route is wrong."
    ),
}

DIAG_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.diag.ping",
        handler_attr="diag_ping",
        summary="Ping a host from the firewall, from one of the firewall's own addresses.",
        description=(
            "Runs a short ping (1 to 5 packets) on the pfSense firewall from a "
            "source address that must belong to the firewall, and returns "
            "sent, received and loss. Proves that a route works. Changes "
            "nothing."
        ),
        parameter_schema=_PING_PARAMETER_SCHEMA,
        response_schema=_PING_RESPONSE_SCHEMA,
        group_key="diagnostics",
        tags=("read-only", "diagnostics", "ping", "routing", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_PING_LLM_INSTRUCTIONS,
    ),
)
