# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time blast-radius preview builder for ``windns.record.remove`` (#3288).

The windows_dns arm of the governed-delete tier. ``windns.record.remove`` was
promoted to ``safety_level=destructive`` + ``requires_approval=True`` by the
#3288 operator ruling, mirroring how #3247 governed ``bind9.record.remove``.
This module wires the mandatory blast-radius statement onto the per-op builder
hook shipped by #1437 (:mod:`meho_backplane.operations._preview`) — the same
shape the bind9 (:mod:`~meho_backplane.connectors.bind9.ops_record_delete_preview`)
and ArgoCD (:mod:`~meho_backplane.connectors.argocd.ops_write_preview`) previews
use.

The builder is **read-only**: it runs ``Get-DnsServerResourceRecord`` scoped to
the same ``(zone, name, RRType)`` the remove handler will clear, projects each
matching record to a compact ``{type, rdata}`` pair, and never mutates. It
returns a ``{"blast_radius": {...}}`` sub-dict the dispatcher promotes to the
top of ``ApprovalRequest.proposed_effect`` (#3197), so a ``destructive`` op
cannot park without the approver seeing *exactly which records die*:

* ``object`` — the record-set identity ``{kind: "dns_record", zone, name,
  type}``. ``windns.record.remove`` is scoped to one RRType (unlike bind9's
  whole-name clear across A + AAAA), so the object names a single type.
* ``children`` — every current value at that ``(zone, name, type)`` that the
  ``-Force`` clear removes, each ``{kind: "record_value", type, rdata}`` (empty
  is a valid "no such record currently" statement — the ``-Force`` clear then
  no-ops post-approval; more than one is the multi-value RRset the whole-name
  clear removes together).
* ``irreversibility`` — ``"recreatable"``: a removed DNS record can be re-added
  from the captured ``rdata`` (via ``windns.record.add`` for A / CNAME, or the
  vendor tooling for other types), unlike a destroyed VM disk. The class is
  honest so the approver weighs the real cost (live resolution breaks
  immediately for every consumer) against the recovery path.

Declines (returns ``None`` → identifier-only default → the park is refused
``blast_radius_required``, fail-closed) when the connector/target is unresolved,
when ``type`` is out of the removable set, or when the read fails (a
:class:`~meho_backplane.connectors._shared.pwsh.PwshRunError` — the "cannot see
current state → refuse at park time" contract, matching bind9's decline on a
zonefile read failure).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import PwshRunError, ps_single_quote, pwsh_run
from meho_backplane.connectors.windows_dns.connector import WindowsDnsConnector
from meho_backplane.connectors.windows_dns.ops_record import _REMOVE_SUPPORTED_TYPES
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

_REMOVE_OP_ID = "windns.record.remove"

#: Read-only projection body: ``ForEach-Object`` over the read records,
#: projecting each record's type-specific ``RecordData`` to a flat ``rdata``
#: string, then a ``@{ rows = ...; total = ... }`` envelope that keeps stdout
#: non-empty and JSON-shaped even for a zero-match read (the same
#: empty-stdout-guard discipline the ``record.get`` handler uses). Kept as a
#: brace-heavy raw literal (PowerShell hashtables + subexpressions), so the Get
#: line below is concatenated in — never ``str.format``, which would trip on the
#: literal ``{`` / ``}``.
_PROJECTION_BODY = (
    "$rows = @($recs | ForEach-Object { "
    "$rd = $_.RecordData; "
    "switch ($_.RecordType.ToString()) { "
    "'A' { $val = $rd.IPv4Address.IPAddressToString } "
    "'AAAA' { $val = $rd.IPv6Address.IPAddressToString } "
    "'CNAME' { $val = $rd.HostNameAlias } "
    "'PTR' { $val = $rd.PtrDomainName } "
    "'NS' { $val = $rd.NameServer } "
    "'MX' { $val = \"$($rd.Preference) $($rd.MailExchange)\" } "
    "'TXT' { $val = ($rd.DescriptiveText -join ' ') } "
    "'SRV' { $val = \"$($rd.Priority) $($rd.Weight) $($rd.Port) $($rd.DomainName)\" } "
    "default { $val = ($rd | Out-String).Trim() } "
    "} "
    '@{ type = $_.RecordType.ToString(); rdata = "$val" } '
    "}); "
    "ConvertTo-Json -Depth 4 -InputObject @{ rows = $rows; total = $rows.Count }"
)


def _build_read_script(*, zone: str, name: str, record_type: str) -> str:
    """Compose the read-only ``Get-DnsServerResourceRecord`` projection script.

    A ``SilentlyContinue`` preference so a missing zone / no-match read yields an
    empty array rather than a terminating error. Operator-supplied scalars are
    single-quoted PowerShell literals (:func:`ps_single_quote`).
    """
    get_expr = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        f"$recs = @(Get-DnsServerResourceRecord -ZoneName {ps_single_quote(zone)} "
        f"-Name {ps_single_quote(name)} -RRType {ps_single_quote(record_type)}); "
    )
    return get_expr + _PROJECTION_BODY


def _child_values(payload: Any, *, record_type: str) -> list[dict[str, Any]]:
    """Normalise the projection payload into the ``children`` list.

    ``ConvertTo-Json`` renders a single-element array as a bare object and a
    zero-element array as ``null`` inside the envelope, so the rows field is
    normalised to a list before projection. Each row's ``rdata`` is coerced to
    ``str`` and its ``type`` defaulted to the requested ``record_type`` (the
    read is already RRType-scoped, so every row is that type).
    """
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if rows is None:
        rows = []
    elif isinstance(rows, dict) or not isinstance(rows, list):
        rows = [rows]
    children: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rdata = row.get("rdata")
        children.append(
            {
                "kind": "record_value",
                "type": str(row.get("type") or record_type),
                "rdata": "" if rdata is None else str(rdata),
            }
        )
    return children


async def _windns_record_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the mandatory destructive-tier blast radius for ``record.remove``.

    Read-only. See the module docstring for the shape and the decline contract.
    Never issues a write — the ``-Force`` clear stays parked until a human
    approves.
    """
    connector = ctx.connector_instance
    if not isinstance(connector, WindowsDnsConnector) or ctx.target is None:
        return None
    zone = ctx.params.get("zone")
    name = ctx.params.get("name")
    record_type = ctx.params.get("type")
    if not (isinstance(zone, str) and isinstance(name, str) and isinstance(record_type, str)):
        return None
    record_type = record_type.upper()
    if record_type not in _REMOVE_SUPPORTED_TYPES:
        # An out-of-set type declines the preview rather than build a blast
        # radius the handler would reject at execution time anyway.
        return None

    script = _build_read_script(zone=zone, name=name, record_type=record_type)
    operator: Operator | None = ctx.operator
    try:
        payload = await pwsh_run(connector, ctx.target, script, operator=operator)
    except PwshRunError:
        # Cannot read the current record set → decline → the park is refused
        # blast_radius_required (fail-closed). Mirrors bind9's decline on a
        # zonefile read failure.
        return None

    children = _child_values(payload, record_type=record_type)
    return {
        "blast_radius": {
            "object": {
                "kind": "dns_record",
                "zone": zone,
                "name": name,
                "type": record_type,
            },
            "children": children,
            "irreversibility": "recreatable",
            "match_count": len(children),
        },
    }


def _register_windns_remove_preview_builder() -> None:
    """Wire the ``windns.record.remove`` park-time preview builder. Import-time.

    Idempotent (``register_preview_builder`` overwrites), so a test reload is a
    no-op-equivalent — same contract as the bind9 / ArgoCD wirings.
    """
    register_preview_builder(_REMOVE_OP_ID, _windns_record_remove_preview)


_register_windns_remove_preview_builder()
