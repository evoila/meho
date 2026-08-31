# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time blast-radius preview builder for ``bind9.record.delete`` (#3231).

The bind9 arm of the governed-delete tier (decision
``docs/decisions/governed-delete-operations.md``, requirement 3) wires the one
op that can compute a mandatory blast-radius statement onto the per-op builder
hook shipped by #1437 (:mod:`meho_backplane.operations._preview`) — mirroring
how :mod:`meho_backplane.connectors.argocd.ops_write_preview` wires ArgoCD's
write previews.

The builder is **read-only**: it resolves the owning zone
(``named-checkconf -p``) and reads the current zonefile (``cat``) via the same
helpers the handler uses, but never stages, reloads, or deletes. It returns a
``{"blast_radius": {...}}`` sub-dict that the dispatcher promotes to the top of
``ApprovalRequest.proposed_effect`` (#3197), so a ``destructive`` op cannot
park without the approver seeing *exactly which record dies*:

* ``object`` — the record identity ``{kind, zone, name, type, view, rdata?}``.
* ``children`` — the record's current sibling values at that name/type, each
  ``{kind: "record_value", type, rdata}`` (empty is a valid "no such record
  currently" statement — the handler then refuses ``not_found`` post-approval;
  more than one with no ``rdata`` in params is the ``ambiguous`` shape).
* ``irreversibility`` — ``"recreatable"``: a deleted DNS record can be
  re-added from the captured ``rdata`` (via ``bind9.record.add``), unlike a
  destroyed VM disk. The class is honest so the approver weighs the real cost
  (live resolution breaks immediately for every consumer) against the recovery
  path.

Declines (returns ``None`` → identifier-only default → the park is refused
``blast_radius_required``, fail-closed) when the connector/target is
unresolved, when the zone is not one this server writably serves (a
``ZoneResolutionError`` — the "zone not managed → refuse" contract at park
time), or when the zonefile cannot be read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.bind9.connector import Bind9Connector
from meho_backplane.connectors.bind9.ops_record import (
    RemoteCommandError,
    ZoneResolutionError,
    _find_record_matches,
    _read_zonefile_text,
    _resolve_zone_and_path,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

if TYPE_CHECKING:
    import dns.exception  # noqa: F401 -- referenced only in the except clause below

_DELETE_OP_ID = "bind9.record.delete"

#: Only A / AAAA are deletable (mirrors ``ops_record._WRITE_SUPPORTED_TYPES``);
#: an out-of-set type declines the preview rather than build a blast radius the
#: handler would reject.
_PREVIEWABLE_TYPES: frozenset[str] = frozenset({"A", "AAAA"})


async def _bind9_record_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the mandatory destructive-tier blast radius for ``record.delete``.

    Read-only. See the module docstring for the shape and the decline
    contract. Never issues a write — the delete stays parked until a human
    approves.
    """
    connector = ctx.connector_instance
    if not isinstance(connector, Bind9Connector) or ctx.target is None:
        return None
    fqdn = ctx.params.get("fqdn")
    record_type = ctx.params.get("type")
    if not isinstance(fqdn, str) or not isinstance(record_type, str):
        return None
    record_type = record_type.upper()
    if record_type not in _PREVIEWABLE_TYPES:
        return None
    rdata_param = ctx.params.get("rdata")
    explicit_zone = ctx.params.get("zone")
    explicit_view = ctx.params.get("view")

    try:
        zone_name, zonefile_path, view = await _resolve_zone_and_path(
            connector,
            ctx.target,
            fqdn=fqdn,
            explicit_zone=explicit_zone if isinstance(explicit_zone, str) else None,
            explicit_view=explicit_view if isinstance(explicit_view, str) else None,
            operator=ctx.operator,
        )
    except (ZoneResolutionError, RemoteCommandError):
        # Zone not managed here / named-checkconf failed → decline → the park
        # is refused blast_radius_required (fail-closed). The handler also
        # re-refuses unmanaged_zone post-approval as defence-in-depth.
        return None

    try:
        current_text = await _read_zonefile_text(connector, ctx.target, zonefile_path, ctx.operator)
    except RemoteCommandError:
        return None

    import dns.exception

    try:
        matches = _find_record_matches(
            current_text, zone_name=zone_name, fqdn=fqdn, record_type=record_type
        )
    except dns.exception.DNSException:
        return None

    obj: dict[str, Any] = {
        "kind": "dns_record",
        "zone": zone_name,
        "name": fqdn if fqdn.endswith(".") else fqdn + ".",
        "type": record_type,
        "view": view,
    }
    if isinstance(rdata_param, str) and rdata_param:
        obj["rdata"] = rdata_param
    children = [{"kind": "record_value", "type": record_type, "rdata": value} for value in matches]
    return {
        "blast_radius": {
            "object": obj,
            "children": children,
            "irreversibility": "recreatable",
            "match_count": len(matches),
        },
    }


def _register_bind9_delete_preview_builder() -> None:
    """Wire the ``bind9.record.delete`` park-time preview builder. Import-time.

    Idempotent (``register_preview_builder`` overwrites), so a test reload is
    a no-op-equivalent — same contract as the ArgoCD / vmware wirings.
    """
    register_preview_builder(_DELETE_OP_ID, _bind9_record_delete_preview)


_register_bind9_delete_preview_builder()
