# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""windows_dns server forwarders -- read (``forwarder.list``) and replace (``forwarder.set``).

A DNS server sends the questions it cannot answer itself to its forwarders.
``Set-DnsServerForwarder`` replaces the whole forwarder list, so
``forwarder.set`` is safe to run twice: the same list gives the same result.
After the change the handler reads the settings back and returns them.

Injection safety: each address is checked with :mod:`ipaddress` and written
back in its normal form inside a single-quoted literal, so only an IP address
can reach the script. ``use_root_hint`` becomes the fixed literal ``$true`` or
``$false``.

References
----------

* ``Get-DnsServerForwarder`` / ``Set-DnsServerForwarder``:
  https://learn.microsoft.com/en-us/powershell/module/dnsserver/set-dnsserverforwarder
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.windows_dns.ops import SSH_TRANSPORT_NOTE, WindowsDnsOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector

__all__ = [
    "FORWARDER_OPS",
    "MAX_FORWARDERS",
    "normalise_forwarders",
    "windows_dns_forwarder_list",
    "windows_dns_forwarder_set",
]

#: Most forwarders one call may set. A server rarely needs more than a few.
MAX_FORWARDERS: int = 8

_READ_SCRIPT: str = (
    "$f = Get-DnsServerForwarder; "
    "ConvertTo-Json -Compress -Depth 3 -InputObject @{ "
    "ip_addresses = @($f.IPAddress | ForEach-Object { $_.IPAddressToString }); "
    "use_root_hint = [bool]$f.UseRootHint; "
    "timeout_seconds = [int]$f.Timeout; "
    "enable_reordering = [bool]$f.EnableReordering }"
)


def normalise_forwarders(raw: Any) -> list[str]:
    """Check *raw* is a list of 1 to 8 distinct IP addresses; return them in normal form."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_FORWARDERS:
        raise ValueError(f"ip_addresses must be a list of 1 to {MAX_FORWARDERS} IP addresses")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"forwarder {item!r} is not a string")
        try:
            address = str(ipaddress.ip_address(item.strip()))
        except ValueError as exc:
            raise ValueError(f"forwarder {item!r} is not an IP address") from exc
        if address in out:
            raise ValueError(f"forwarder {address} is in the list twice")
        out.append(address)
    return out


def _forwarder_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("the forwarder read did not return a JSON object")
    addresses = payload.get("ip_addresses")
    if isinstance(addresses, str):
        addresses = [addresses]
    return {**payload, "ip_addresses": addresses if isinstance(addresses, list) else []}


async def windows_dns_forwarder_list(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.forwarder.list`` -- the server's forwarders and their settings."""
    del params
    script = "$ErrorActionPreference = 'Stop'; " + _READ_SCRIPT
    return _forwarder_dict(await pwsh_run(connector, target, script, operator=operator))


async def windows_dns_forwarder_set(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.forwarder.set`` -- replace the forwarder list."""
    addresses = normalise_forwarders(params.get("ip_addresses"))
    use_root_hint = params.get("use_root_hint")
    if use_root_hint is not None and not isinstance(use_root_hint, bool):
        raise ValueError("use_root_hint must be true or false")

    address_literal = "@(" + ", ".join(ps_single_quote(a) for a in addresses) + ")"
    root_hint_clause = ""
    if use_root_hint is not None:
        root_hint_clause = " -UseRootHint " + ("$true" if use_root_hint else "$false")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Set-DnsServerForwarder -IPAddress {address_literal}{root_hint_clause}; " + _READ_SCRIPT
    )
    result = _forwarder_dict(await pwsh_run(connector, target, script, operator=operator))
    return {**result, "op_class": "write"}


_FORWARDER_RESPONSE_PROPERTIES: dict[str, Any] = {
    "ip_addresses": {
        "type": "array",
        "items": {"type": "string"},
        "description": "The forwarders, in the order the server asks them.",
    },
    "use_root_hint": {
        "type": "boolean",
        "description": "The server asks the root servers itself when no forwarder answers.",
    },
    "timeout_seconds": {"type": "integer", "description": "Seconds to wait for one forwarder."},
    "enable_reordering": {
        "type": "boolean",
        "description": "The server may change the order of the forwarders.",
    },
}


FORWARDER_OPS: tuple[WindowsDnsOp, ...] = (
    WindowsDnsOp(
        op_id="windns.forwarder.list",
        handler_attr="windows_dns_forwarder_list",
        summary="Read the DNS server's forwarders and the root-hint setting.",
        description=(
            "Runs Get-DnsServerForwarder and returns the forwarder addresses in order, "
            "whether the server asks the root servers when no forwarder answers, the "
            "timeout and the reordering setting. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema={
            "type": "object",
            "properties": _FORWARDER_RESPONSE_PROPERTIES,
            "required": ["ip_addresses"],
            "additionalProperties": True,
        },
        group_key="forwarder",
        tags=("read-only", "forwarder", "settings"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to check where the DNS server sends names it cannot answer itself. "
                + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{ip_addresses: [str], use_root_hint, timeout_seconds, enable_reordering}."
            ),
            "next_step": "To change the list, call windns.forwarder.set.",
        },
    ),
    WindowsDnsOp(
        op_id="windns.forwarder.set",
        handler_attr="windows_dns_forwarder_set",
        summary="Replace the DNS server's forwarder list.",
        description=(
            "Runs Set-DnsServerForwarder -IPAddress with 1 to 8 addresses, which replaces "
            "the whole list, and can set the root-hint flag. Then reads the forwarders "
            "back. Safe to run twice. A person must approve it."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "ip_addresses": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 2,
                        "description": "One IPv4 or IPv6 address of a DNS server.",
                    },
                    "minItems": 1,
                    "maxItems": MAX_FORWARDERS,
                    "uniqueItems": True,
                    "description": "The new forwarders, in the order the server should ask them.",
                },
                "use_root_hint": {
                    "type": "boolean",
                    "description": (
                        "Optional. false: only ask the forwarders. true: ask the root "
                        "servers when no forwarder answers. Leave it out to keep the value."
                    ),
                },
            },
            "required": ["ip_addresses"],
            "additionalProperties": False,
            "examples": [{"ip_addresses": ["192.0.2.53", "198.51.100.53"]}],
        },
        response_schema={
            "type": "object",
            "properties": {
                **_FORWARDER_RESPONSE_PROPERTIES,
                "op_class": {
                    "type": "string",
                    "enum": ["write"],
                    "description": "Always 'write'.",
                },
            },
            "required": ["ip_addresses", "op_class"],
            "additionalProperties": True,
        },
        group_key="forwarder",
        tags=("write", "forwarder", "settings"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call while building a DNS server to set where it sends names it cannot "
                "answer itself. " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. Read the current list first with "
                "windns.forwarder.list, so you can set it back. The call replaces the "
                "whole list."
            ),
            "parameter_hints": {
                "ip_addresses": "Required. 1 to 8 IP addresses, no host names.",
                "use_root_hint": "Optional. Leave it out to keep the current value.",
            },
            "output_shape": (
                "The windns.forwarder.list fields after the change, plus op_class 'write'."
            ),
            "next_step": "Check that ip_addresses matches the list you sent.",
        },
    ),
)
