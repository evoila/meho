# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense OpenVPN reads -- ``pfsense.openvpn.clients`` and ``pfsense.openvpn.log``.

Two read ops for the first question of a "the VPN is slow" ticket.

``pfsense.openvpn.clients``
    The clients that are connected now, for each OpenVPN server on the
    firewall. The op asks each server's management socket for
    ``status 3`` (the tab-separated status format). It reads the column
    names from the ``HEADER`` line, so a column added by a newer OpenVPN
    does not break it. The protocol (``proto``) and port of each server
    come from its generated config file, with a grep for those two
    words only.

``pfsense.openvpn.log``
    The OpenVPN log lines of one user (common name) in a time window,
    each with an event type: ``connect``, ``disconnect``, ``restart``,
    ``error`` or ``other``.

Bounded on purpose
------------------

* No free text reaches the shell. The commands are fixed strings. The
  only input in a command is the common name, which must match
  ``[A-Za-z0-9._@-]{2,64}`` and is quoted with :func:`shlex.quote` on top.
* The log read returns at most ``limit`` events (default 200, at most
  1000) and reads at most the last 5000 matching lines. When lines may be
  missing, the result says ``truncated: true``.
* A server whose socket cannot be read is reported with
  ``readable: false``. It is never shown as a server with no clients.

Time in the log
---------------

An OpenVPN log line starts with a syslog stamp (``Nov 10 12:34:56``) in the
firewall's local time, with no year and no zone. The op reads the
firewall's own date and UTC offset in the same command, adds the year and
the offset, and compares the lines to the window as exact moments. The
window (``since`` / ``until``) must carry a zone offset or ``Z``.
A stamp that would lie in the future is placed in the year before
(so December lines read in January are right). The offset is the one at call time, so a line
from before a clock change can be off by one hour.

The ops change nothing on the firewall.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "classify_log_line",
    "parse_openvpn_log",
    "parse_openvpn_status",
    "pfsense_openvpn_clients",
    "pfsense_openvpn_log",
]

_LOG_FILE = "/var/log/openvpn.log"
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000
_MAX_LOG_LINES = 5000
_MAX_RAW_CHARS = 500

# A common name: letters, digits, dot, underscore, at sign and dash. The
# first character is a letter or digit so a value can never look like a
# grep option. A name with other characters is refused, not cleaned.
_COMMON_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{1,63}")

_CLIENTS_CMD = (
    "for d in /var/etc/openvpn/server*; do "
    '[ -S "$d/sock" ] || continue; '
    'echo "@@SERVER ${d##*/}"; '
    "grep -E '^(proto|port) ' \"$d/config.ovpn\" | sed 's/^/@@CONF /'; "
    "echo @@STATUS; "
    "printf 'status 3\\nquit\\n' | nc -U -w 3 \"$d/sock\"; "
    "echo @@END; "
    "done"
)

_SYSLOG_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+\S+\s+(?P<msg>.*)$"
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
_RESTART_RE = re.compile(r"SIGUSR1\[(?:soft|hard),([^\]]+)\]")
_STOP_RE = re.compile(r"SIG(?:TERM|INT)\[(?:soft|hard),([^\]]+)\]")
_ERROR_RE = re.compile(
    r"TLS Error|TLS handshake failed|VERIFY ERROR|AUTH_FAILED|"
    r"Authenticate/Decrypt packet error|TLS key negotiation failed",
    re.IGNORECASE,
)
_INACTIVE_RE = re.compile(r"inactivity timeout", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------


def _validate_common_name(value: Any) -> str:
    if not isinstance(value, str) or not _COMMON_NAME_RE.fullmatch(value.strip()):
        raise ValueError(
            "common_name must be 2 to 64 characters: letters, digits, '.', '_', '@' or '-', "
            f"starting with a letter or digit; got {value!r}"
        )
    return value.strip()


def _validate_moment(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 time with a zone; got {value!r}")
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{field} must be an ISO 8601 time with a zone; got {value!r}") from None
    if moment.tzinfo is None:
        raise ValueError(
            f"{field} must carry a zone offset or 'Z' (for example 2026-11-10T09:00:00Z); "
            f"got {value!r}"
        )
    return moment


def _validate_limit(value: Any) -> int:
    if value is None:
        return _DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"limit must be a whole number from 1 to {_MAX_LIMIT}; got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _client_row(header: list[str], cells: list[str]) -> dict[str, Any]:
    by_name = dict(zip(header, cells, strict=False))
    epoch = _to_int(by_name.get("Connected Since (time_t)", ""))
    return {
        "common_name": by_name.get("Common Name"),
        "real_address": by_name.get("Real Address"),
        "virtual_address": by_name.get("Virtual Address") or None,
        "virtual_ipv6_address": by_name.get("Virtual IPv6 Address") or None,
        "bytes_received": _to_int(by_name.get("Bytes Received", "")),
        "bytes_sent": _to_int(by_name.get("Bytes Sent", "")),
        "connected_since": (
            datetime.fromtimestamp(epoch, tz=UTC).isoformat() if epoch is not None else None
        ),
        "username": by_name.get("Username") or None,
    }


def parse_openvpn_status(output: str) -> list[dict[str, Any]]:
    """Parse the per-server blocks that ``_CLIENTS_CMD`` prints.

    Returns one dict per server: ``{server, proto, port, readable,
    clients}``. A server is ``readable`` only when its status ended with
    the ``END`` marker and had a ``CLIENT_LIST`` header, so a dead socket
    never looks like a server with no clients.

    >>> out = (
    ...     "@@SERVER server1\\n@@CONF proto udp4\\n@@CONF port 1194\\n@@STATUS\\n"
    ...     "HEADER\\tCLIENT_LIST\\tCommon Name\\tReal Address\\tVirtual Address\\n"
    ...     "CLIENT_LIST\\talice\\t203.0.113.5:4000\\t198.51.100.2\\n"
    ...     "END\\n@@END\\n"
    ... )
    >>> parse_openvpn_status(out)[0]["clients"][0]["common_name"]
    'alice'
    """
    servers: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    header: list[str] | None = None
    saw_end = False
    for raw in output.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("@@SERVER "):
            current = {
                "server": line[len("@@SERVER ") :].strip(),
                "proto": None,
                "port": None,
                "readable": False,
                "clients": [],
            }
            header, saw_end = None, False
            servers.append(current)
        elif current is None:
            continue
        elif line.startswith("@@CONF "):
            key, _, value = line[len("@@CONF ") :].partition(" ")
            if key in ("proto", "port"):
                current[key] = value.strip() or None
        elif line == "@@END":
            current["readable"] = header is not None and saw_end
        elif line.startswith("HEADER\tCLIENT_LIST\t"):
            header = line.split("\t")[2:]
        elif line.startswith("CLIENT_LIST\t") and header is not None:
            current["clients"].append(_client_row(header, line.split("\t")[1:]))
        elif line == "END":
            saw_end = True
    return servers


def classify_log_line(message: str) -> dict[str, Any]:
    """Return ``{event, reason}`` for the message part of one log line.

    >>> classify_log_line("alice/203.0.113.5:4000 SIGUSR1[soft,ping-restart] received")
    {'event': 'restart', 'reason': 'ping-restart'}
    """
    if "Peer Connection Initiated" in message:
        return {"event": "connect", "reason": None}
    restart = _RESTART_RE.search(message)
    if restart:
        return {"event": "restart", "reason": restart.group(1)}
    if _INACTIVE_RE.search(message):
        return {"event": "disconnect", "reason": "inactivity-timeout"}
    stop = _STOP_RE.search(message)
    if stop:
        return {"event": "disconnect", "reason": stop.group(1)}
    if _ERROR_RE.search(message):
        return {"event": "error", "reason": None}
    return {"event": "other", "reason": None}


def _stamp_to_moment(mon: str, day: str, clock: str, now: datetime) -> datetime | None:
    """Place a year-less syslog stamp in the year that is not in the future."""
    month = _MONTHS.get(mon)
    if month is None:
        return None
    hour, minute, second = (int(part) for part in clock.split(":"))
    try:
        moment = datetime(now.year, month, int(day), hour, minute, second, tzinfo=now.tzinfo)
        if moment > now + timedelta(days=1):
            moment = moment.replace(year=now.year - 1)
    except ValueError:
        return None
    return moment


def _parse_firewall_now(text: str) -> datetime | None:
    """Parse ``date '+%Y-%m-%d %H:%M:%S %z'`` output, for example ``2026-11-10 09:30:00 +0100``."""
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})", text.strip()
    )
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    offset = timedelta(hours=int(match.group(8)), minutes=int(match.group(9)))
    zone = timezone(-offset if match.group(7) == "-" else offset)
    return datetime(year, month, day, hour, minute, second, tzinfo=zone)


def parse_openvpn_log(
    lines: list[str],
    now: datetime,
    since: datetime | None,
    until: datetime | None,
) -> list[dict[str, Any]]:
    """Turn log lines into events inside the window, oldest first.

    A line that has no readable syslog stamp is kept only when no window
    is set, with ``time: null``; with a window it cannot be placed, so it
    is left out.
    """
    events: list[dict[str, Any]] = []
    for line in lines:
        match = _SYSLOG_RE.match(line)
        moment = (
            _stamp_to_moment(match.group("mon"), match.group("day"), match.group("time"), now)
            if match
            else None
        )
        if moment is None and (since is not None or until is not None):
            continue
        if moment is not None and (
            (since is not None and moment < since) or (until is not None and moment > until)
        ):
            continue
        message = match.group("msg") if match else line
        events.append(
            {
                "time": moment.isoformat() if moment is not None else None,
                **classify_log_line(message),
                "line": line[:_MAX_RAW_CHARS],
            }
        )
    return events


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _text(proc: Any, attr: str) -> str:
    value = getattr(proc, attr, "") or ""
    return value if isinstance(value, str) else ""


def _first_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[0][:200]


async def pfsense_openvpn_clients(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.clients`` -- the connected clients now.

    Returns ``{servers: [{server, proto, port, readable, clients}],
    client_count}``. ``client_count`` counts only the servers that could
    be read. Raises :exc:`RuntimeError` when the command fails.
    """
    proc = await self._run_command(target, _CLIENTS_CMD, operator=operator, timeout=30.0)
    if proc.exit_status != 0:
        detail = _first_line(_text(proc, "stderr"))
        message = f"openvpn status read failed: exit {proc.exit_status}"
        raise RuntimeError(f"{message}: {detail}" if detail else message)
    servers = parse_openvpn_status(_text(proc, "stdout"))
    return {
        "servers": servers,
        "client_count": sum(len(s["clients"]) for s in servers if s["readable"]),
    }


async def pfsense_openvpn_log(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``pfsense.openvpn.log`` -- log events of one user in a window.

    Returns ``{common_name, since, until, event_count, truncated,
    events}``. Raises :exc:`ValueError` on bad input and
    :exc:`RuntimeError` when the log cannot be read.
    """
    common_name = _validate_common_name(params.get("common_name"))
    since = _validate_moment(params.get("since"), "since")
    until = _validate_moment(params.get("until"), "until")
    if since is not None and until is not None and since > until:
        raise ValueError("since must not be later than until")
    limit = _validate_limit(params.get("limit"))

    cmd = (
        f"date '+%Y-%m-%d %H:%M:%S %z'; "
        f"[ -r {_LOG_FILE} ] || {{ echo '@@NOLOG'; exit 3; }}; "
        f"grep -F -e {shlex.quote(common_name)} {_LOG_FILE} | tail -n {_MAX_LOG_LINES}"
    )
    proc = await self._run_command(target, cmd, operator=operator, timeout=30.0)
    stdout = _text(proc, "stdout")
    if proc.exit_status != 0:
        reason = "the OpenVPN log file is not readable" if "@@NOLOG" in stdout else "read failed"
        detail = _first_line(_text(proc, "stderr"))
        message = f"openvpn log read failed ({reason}): exit {proc.exit_status}"
        raise RuntimeError(f"{message}: {detail}" if detail else message)
    first, _, rest = stdout.partition("\n")
    now = _parse_firewall_now(first)
    if now is None:
        raise RuntimeError("openvpn log read failed: the firewall date could not be read")
    lines = [ln for ln in rest.splitlines() if ln.strip()]
    events = parse_openvpn_log(lines, now, since, until)
    # 5000 lines means the tail cut older lines, so the window may be incomplete.
    truncated = len(lines) >= _MAX_LOG_LINES or len(events) > limit
    return {
        "common_name": common_name,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "event_count": min(len(events), limit),
        "truncated": truncated,
        "events": events[-limit:],
    }
