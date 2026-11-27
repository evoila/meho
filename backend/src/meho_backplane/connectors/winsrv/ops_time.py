# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv time source -- read (``time.get``) and change (``time.set``).

The Windows Time service (W32Time) decides where a server gets its time. On
the first domain controller of a forest (the PDC emulator) this is the time
source of the whole domain, so a build sets it to known NTP servers.

* ``time.get`` reads the settings from the registry
  (``W32Time\\Parameters`` ``NtpServer`` and ``Type``, ``W32Time\\Config``
  ``AnnounceFlags``) and the source the service uses now
  (``w32tm /query /source``).
* ``time.set`` runs ``w32tm /config /manualpeerlist:<peers>
  /syncfromflags:manual [/reliable:yes|no] /update`` and reads the settings
  back. The same peers give the same result, so it is safe to run twice.

Injection safety: each peer must match :data:`PEER_PATTERN` (a host name or
IPv4 address, with an optional ``,0x<flags>`` suffix), so a peer can hold no
space, quote or PowerShell sign. The joined list is still passed as a
single-quoted literal. ``w32tm`` is a program, not a cmdlet, so the script
checks ``$LASTEXITCODE`` and throws when it is not 0.

References
----------

* Windows Time service tools and settings (``w32tm /config``, the registry
  values): https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "MAX_PEERS",
    "PEER_PATTERN",
    "TIME_OPS",
    "check_peers",
    "winsrv_time_get",
    "winsrv_time_set",
]

#: Most NTP peers one call may set.
MAX_PEERS: int = 5

#: One NTP peer: a host name or IPv4 address, with an optional
#: ``,0x<flags>`` suffix (for example ``,0x8`` for client mode).
PEER_PATTERN: str = r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?(?:,0x[0-9A-Fa-f]{1,2})?$"
_PEER_RE = re.compile(PEER_PATTERN)

#: AnnounceFlags bit 0x04: the server always tells clients it is a reliable
#: time source (what ``/reliable:yes`` sets).
_ALWAYS_RELIABLE_FLAG: int = 0x04

_READ_SCRIPT: str = (
    "$p = Get-ItemProperty -LiteralPath "
    "'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\W32Time\\Parameters'; "
    "$c = Get-ItemProperty -LiteralPath "
    "'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\W32Time\\Config'; "
    "$src = (& w32tm /query /source | Out-String).Trim(); "
    "if ($LASTEXITCODE -ne 0) { $src = $null }; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "ntp_server = [string]$p.NtpServer; "
    "type = [string]$p.Type; "
    "announce_flags = [int]$c.AnnounceFlags; "
    "source = $src }"
)


def check_peers(raw: Any) -> list[str]:
    """Check *raw* is a list of 1 to 5 distinct, well-formed NTP peers."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PEERS:
        raise ValueError(f"peers must be a list of 1 to {MAX_PEERS} NTP servers")
    out: list[str] = []
    for peer in raw:
        if not isinstance(peer, str) or not _PEER_RE.fullmatch(peer):
            raise ValueError(
                f"peer {peer!r} must be a host name or IPv4 address, optionally "
                "followed by ',0x<flags>'"
            )
        if peer in out:
            raise ValueError(f"peer {peer!r} is in the list twice")
        out.append(peer)
    return out


def _time_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("the time read did not return a JSON object")
    flags = payload.get("announce_flags")
    reliable = isinstance(flags, int) and bool(flags & _ALWAYS_RELIABLE_FLAG)
    ntp_server = payload.get("ntp_server")
    peers = ntp_server.split() if isinstance(ntp_server, str) else []
    return {**payload, "peers": peers, "reliable": reliable}


async def winsrv_time_get(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.time.get`` -- the time settings and the current source."""
    del params
    script = "$ErrorActionPreference = 'Stop'; " + _READ_SCRIPT
    return _time_dict(await pwsh_run(connector, target, script, operator=operator))


async def winsrv_time_set(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.time.set`` -- set the NTP peers (and the reliable flag)."""
    peers = check_peers(params.get("peers"))
    reliable = params.get("reliable")
    if reliable is not None and not isinstance(reliable, bool):
        raise ValueError("reliable must be true or false")
    reliable_arg = ""
    if reliable is not None:
        reliable_arg = " /reliable:yes" if reliable else " /reliable:no"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$peers = {ps_single_quote(' '.join(peers))}; "
        '$out = & w32tm /config "/manualpeerlist:$peers" /syncfromflags:manual'
        f"{reliable_arg} /update | Out-String; "
        "if ($LASTEXITCODE -ne 0) { throw ('w32tm /config failed: ' + $out.Trim()) }; "
        + _READ_SCRIPT
    )
    result = _time_dict(await pwsh_run(connector, target, script, operator=operator))
    return {**result, "op_class": "write"}


_TIME_RESPONSE_PROPERTIES: dict[str, Any] = {
    "ntp_server": {
        "type": "string",
        "description": "The raw NtpServer registry value (peers with their flags).",
    },
    "peers": {
        "type": "array",
        "items": {"type": "string"},
        "description": "The NtpServer value split into peers.",
    },
    "type": {
        "type": "string",
        "description": (
            "Where the service takes time from: NTP (the peers), NT5DS (the domain), "
            "NoSync or AllSync."
        ),
    },
    "announce_flags": {"type": "integer", "description": "The raw AnnounceFlags registry value."},
    "reliable": {
        "type": "boolean",
        "description": "The server always tells clients it is a reliable time source.",
    },
    "source": {
        "type": ["string", "null"],
        "description": (
            "The source the service uses now (w32tm /query /source); null when the query fails."
        ),
    },
}


TIME_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.time.get",
        handler_attr="winsrv_time_get",
        summary="Read where the server gets its time: NTP peers, sync type and reliable flag.",
        description=(
            "Reads the Windows Time settings from the registry (NtpServer, Type, "
            "AnnounceFlags) and the source the service uses now (w32tm /query /source). "
            "Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema={
            "type": "object",
            "properties": _TIME_RESPONSE_PROPERTIES,
            "required": ["peers", "type", "reliable"],
            "additionalProperties": True,
        },
        group_key="time",
        tags=("read-only", "time", "ntp"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to check the time source of a server, for example of the first "
                "domain controller, which gives the time to the whole domain. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{ntp_server, peers: [str], type, announce_flags, reliable, source}.",
            "next_step": "To change the peers, call winsrv.time.set.",
        },
    ),
    WinsrvOp(
        op_id="winsrv.time.set",
        handler_attr="winsrv_time_set",
        summary="Set the server's NTP peers with w32tm /config, and optionally the reliable flag.",
        description=(
            "Runs w32tm /config /manualpeerlist:<peers> /syncfromflags:manual "
            "[/reliable:yes|no] /update, then reads the time settings back. Safe to run "
            "twice. A person must approve it."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "peers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": PEER_PATTERN,
                        "description": (
                            "One NTP server: a host name or IPv4 address, optionally "
                            "with ',0x<flags>', for example ',0x8' for client mode."
                        ),
                    },
                    "minItems": 1,
                    "maxItems": MAX_PEERS,
                    "uniqueItems": True,
                    "description": "The NTP servers to take time from, 1 to 5.",
                },
                "reliable": {
                    "type": "boolean",
                    "description": (
                        "Optional. true: the server tells clients it is a reliable time "
                        "source (use on the first domain controller). Leave it out to "
                        "keep the value."
                    ),
                },
            },
            "required": ["peers"],
            "additionalProperties": False,
            "examples": [{"peers": ["ntp1.example.test,0x8", "192.0.2.123,0x8"], "reliable": True}],
        },
        response_schema={
            "type": "object",
            "properties": {
                **_TIME_RESPONSE_PROPERTIES,
                "op_class": {
                    "type": "string",
                    "enum": ["write"],
                    "description": "Always 'write'.",
                },
            },
            "required": ["peers", "type", "reliable", "op_class"],
            "additionalProperties": True,
        },
        group_key="time",
        tags=("write", "time", "ntp"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call while building a server, for example the first domain controller, "
                "to set the NTP servers it takes time from. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. Read the current values first with "
                "winsrv.time.get, so you can set them back. The server must reach the "
                "peers on UDP port 123."
            ),
            "parameter_hints": {
                "peers": "Required. 1 to 5 host names or IPv4 addresses, each maybe with ',0x8'.",
                "reliable": "Optional. true on the first domain controller of a forest.",
            },
            "output_shape": "The winsrv.time.get fields after the change, plus op_class 'write'.",
            "next_step": (
                "Check that peers and type (NTP) match. The source can take a minute to "
                "change; call winsrv.time.get again to see it."
            ),
        },
    ),
)
