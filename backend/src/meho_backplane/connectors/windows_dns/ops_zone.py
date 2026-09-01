# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""windows_dns zone ops -- read (``zone.list``).

Mirrors :mod:`~meho_backplane.connectors.bind9.ops_zone`'s read surface
but swaps ``named-checkconf -p`` for the Windows ``DnsServer`` module's
``Get-DnsServerZone`` cmdlet, routed through the PowerShell-over-SSH
transport (:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`).

Only the zone *inventory* read is implemented here (the operator-relevant
"what zones does this server host?" question, matching bind9's
``bind9.zone.list``). Zone *creation* / deletion is out of scope for the
mirror — like bind9, the connector's write surface is record-scoped.

References
----------

* ``Get-DnsServerZone``:
  https://learn.microsoft.com/en-us/powershell/module/dnsserver/get-dnsserverzone
* Read-surface sibling: :mod:`meho_backplane.connectors.bind9.ops_zone`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.windows_dns.ops import WindowsDnsOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector

__all__ = [
    "WINDOWS_DNS_ZONE_LIST_LLM_INSTRUCTIONS",
    "WINDOWS_DNS_ZONE_LIST_PARAMETER_SCHEMA",
    "ZONE_OPS",
    "normalise_json_rows",
    "windows_dns_zone_list",
]


def normalise_json_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise a ``ConvertTo-Json`` payload into a list of row dicts.

    PowerShell's ``ConvertTo-Json`` renders a **single**-element result
    as a flat object and a **multi**-element result as a JSON array; a
    zero-element result renders as ``null`` (an empty stdout is caught
    upstream by :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`
    before it reaches here). This helper collapses all three shapes to
    a ``list[dict]`` so handlers walk a uniform structure. Mirrors the
    dict/list normalisation the Holodeck ``probe`` and ``pod.list``
    handlers apply to their cmdlet output.
    """
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


async def windows_dns_zone_list(
    connector: WindowsDnsConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``windns.zone.list``.

    Runs ``Get-DnsServerZone`` on the Windows host via the pwsh helper
    and returns ``{rows, total}``. ``rows`` is the list of zone objects
    the cmdlet emitted (each carrying ``ZoneName``, ``ZoneType``,
    ``IsDsIntegrated``, ``IsReverseLookupZone``, …); ``total`` is the row
    count. Read-only; ``Get-DnsServerZone`` never mutates server state.

    The script wraps the cmdlet output in the same
    ``@{ rows = ...; total = ... }`` hashtable envelope ``record.get``
    uses, so a zone-less server yields the documented
    ``{rows: [], total: 0}`` instead of an empty stdout tripping the
    pwsh helper's guard. Unlike ``record.get`` (where a missing zone is
    a legitimate empty read under ``SilentlyContinue``), the envelope
    runs under ``$ErrorActionPreference = 'Stop'``: ``Get-DnsServerZone``
    takes no narrowing parameters here, so any cmdlet error (DNS service
    stopped, insufficient rights) is a real failure that must terminate
    the process non-zero and surface as a
    :class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`
    carrying the actual stderr -- not be swallowed into a false empty
    inventory.
    """
    del params  # declared empty in schema; intentionally ignored
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$zones = @(Get-DnsServerZone); "
        "ConvertTo-Json -Depth 4 -InputObject @{ rows = $zones; total = $zones.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


WINDOWS_DNS_ZONE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_WINDOWS_DNS_ZONE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


WINDOWS_DNS_ZONE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'what zones does this Windows DNS "
        "server host?' or needs the zone inventory before drilling into "
        "records. Read-only. Returns one row per zone with the zone name, "
        "type (Primary / Secondary / Stub / Forwarder), AD-integration "
        "flag, and reverse-lookup flag. Pair with ``windns.record.get`` "
        "once a zone is identified to query records inside it."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'rows': [{ZoneName, ZoneType, IsDsIntegrated, "
        "IsReverseLookupZone, ...}], 'total': <int>}. ``rows`` is empty "
        "only if the server hosts no zones."
    ),
}


ZONE_OPS: tuple[WindowsDnsOp, ...] = (
    WindowsDnsOp(
        op_id="windns.zone.list",
        handler_attr="windows_dns_zone_list",
        summary="List the zones the Windows DNS server hosts via ``Get-DnsServerZone``.",
        description=(
            "Runs ``Get-DnsServerZone`` on the Windows host and returns "
            "one row per hosted zone (zone name, type, AD-integration "
            "flag, reverse-lookup flag). A zone-less server returns "
            "``rows: []``. Read-only; never mutates server state. The "
            "right op when the agent doesn't yet know which zone to "
            "target, or needs the zone-level context before drilling "
            "into records."
        ),
        parameter_schema=WINDOWS_DNS_ZONE_LIST_PARAMETER_SCHEMA,
        response_schema=_WINDOWS_DNS_ZONE_LIST_RESPONSE_SCHEMA,
        group_key="zone",
        tags=("read-only", "zone", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=WINDOWS_DNS_ZONE_LIST_LLM_INSTRUCTIONS,
    ),
)
