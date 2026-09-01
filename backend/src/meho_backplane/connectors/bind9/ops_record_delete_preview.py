# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time blast-radius preview builders for the bind9 destructive tier.

Two governed-delete-tier ops on the bind9 connector compute a mandatory
blast-radius statement at park time (decision
``docs/decisions/governed-delete-operations.md``, requirement 3), each wired
onto the per-op builder hook shipped by #1437
(:mod:`meho_backplane.operations._preview`) — mirroring how
:mod:`meho_backplane.connectors.argocd.ops_write_preview` wires ArgoCD's write
previews:

* ``bind9.record.delete`` (#3231) — deletes exactly ONE ``(zone, name, type,
  rdata?)`` record. Its blast radius names that single record and enumerates
  the record's sibling values at the same name/type.
* ``bind9.record.remove`` (#3247, promoted to the destructive tier by the
  operator ruling) — clears EVERY A and AAAA record at the name in one write.
  Its blast radius names the whole name and enumerates every A + AAAA value
  that dies.

Both builders are **read-only**: they resolve the owning zone
(``named-checkconf -p``) and read the current zonefile (``cat``) via the same
helpers the handlers use, but never stage, reload, or delete. Each returns a
``{"blast_radius": {...}}`` sub-dict that the dispatcher promotes to the top of
``ApprovalRequest.proposed_effect`` (#3197), so a ``destructive`` op cannot
park without the approver seeing *exactly which record(s) die*:

* ``object`` — the identity of what is removed (a single ``dns_record`` for
  delete; the whole ``dns_name`` for remove).
* ``children`` — the record values that go with it, each
  ``{kind: "record_value", type, rdata}`` (empty is a valid "no such record
  currently" statement — the handlers then act on whatever is present at
  execution time).
* ``irreversibility`` — ``"recreatable"``: a deleted/removed DNS record can be
  re-added from the captured ``rdata`` (via ``bind9.record.add``), unlike a
  destroyed VM disk. The class is honest so the approver weighs the real cost
  (live resolution breaks immediately for every consumer) against the recovery
  path.
* ``match_count`` — the number of record values the approved call will remove.

Both decline (return ``None`` → identifier-only default → the park is refused
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
_REMOVE_OP_ID = "bind9.record.remove"

#: Only A / AAAA are deletable/removable (mirrors
#: ``ops_record._WRITE_SUPPORTED_TYPES``); an out-of-set type declines a
#: single-record ``delete`` preview rather than build a blast radius the
#: handler would reject. ``remove`` iterates this set (it clears both).
_PREVIEWABLE_TYPES: frozenset[str] = frozenset({"A", "AAAA"})


async def _resolve_zone_and_read(
    ctx: PreviewContext, *, fqdn: str
) -> tuple[str, str | None, str] | None:
    """Resolve the owning zone + read its zonefile text at park time, or decline.

    The shared read-only preamble for both bind9 destructive-tier preview
    builders. Resolves the zone via ``named-checkconf -p`` and reads the
    zonefile via ``cat`` — exactly the seams the handlers use — but never
    stages, reloads, or deletes.

    Returns ``(zone_name, view, current_text)`` on success, or ``None`` to
    decline: the connector/target is unresolved, the zone is not writably
    served here (``ZoneResolutionError`` / ``named-checkconf`` failed), or the
    zonefile cannot be read (``RemoteCommandError``). A decline → the caller
    returns ``None`` → the park is refused ``blast_radius_required``
    (fail-closed). The handlers also re-resolve at dispatch time as
    defence-in-depth.
    """
    connector = ctx.connector_instance
    if not isinstance(connector, Bind9Connector) or ctx.target is None:
        return None
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
        return None

    try:
        current_text = await _read_zonefile_text(connector, ctx.target, zonefile_path, ctx.operator)
    except RemoteCommandError:
        return None
    return zone_name, view, current_text


def _absolute_name(fqdn: str) -> str:
    return fqdn if fqdn.endswith(".") else fqdn + "."


async def _bind9_record_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the mandatory destructive-tier blast radius for ``record.delete``.

    Read-only. See the module docstring for the shape and the decline
    contract. Never issues a write — the delete stays parked until a human
    approves.
    """
    fqdn = ctx.params.get("fqdn")
    record_type = ctx.params.get("type")
    if not isinstance(fqdn, str) or not isinstance(record_type, str):
        return None
    record_type = record_type.upper()
    if record_type not in _PREVIEWABLE_TYPES:
        return None
    rdata_param = ctx.params.get("rdata")

    resolved = await _resolve_zone_and_read(ctx, fqdn=fqdn)
    if resolved is None:
        return None
    zone_name, view, current_text = resolved

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
        "name": _absolute_name(fqdn),
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


async def _bind9_record_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the mandatory destructive-tier blast radius for ``record.remove``.

    ``record.remove`` clears EVERY A and AAAA record at the name in one write,
    so the object is the whole ``dns_name`` and the children enumerate both
    families' current values — the full set that dies. Read-only; see the
    module docstring for the decline contract. Never issues a write — the
    remove stays parked until a human approves.
    """
    fqdn = ctx.params.get("fqdn")
    if not isinstance(fqdn, str):
        return None

    resolved = await _resolve_zone_and_read(ctx, fqdn=fqdn)
    if resolved is None:
        return None
    zone_name, view, current_text = resolved

    import dns.exception

    children: list[dict[str, Any]] = []
    try:
        for record_type in ("A", "AAAA"):
            matches = _find_record_matches(
                current_text, zone_name=zone_name, fqdn=fqdn, record_type=record_type
            )
            children.extend(
                {"kind": "record_value", "type": record_type, "rdata": value} for value in matches
            )
    except dns.exception.DNSException:
        return None

    obj: dict[str, Any] = {
        "kind": "dns_name",
        "zone": zone_name,
        "name": _absolute_name(fqdn),
        "types": ["A", "AAAA"],
        "view": view,
    }
    return {
        "blast_radius": {
            "object": obj,
            "children": children,
            "irreversibility": "recreatable",
            "match_count": len(children),
        },
    }


def _register_bind9_delete_preview_builder() -> None:
    """Wire the bind9 destructive-tier park-time preview builders. Import-time.

    Idempotent (``register_preview_builder`` overwrites), so a test reload is
    a no-op-equivalent — same contract as the ArgoCD / vmware wirings.
    """
    register_preview_builder(_DELETE_OP_ID, _bind9_record_delete_preview)
    register_preview_builder(_REMOVE_OP_ID, _bind9_record_remove_preview)


_register_bind9_delete_preview_builder()
