# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builders for the 8 vmware write composites.

G0.22-T3 (#1608). Before this module, a parked ``vmware.composite.*``
write stored only the identifier default ``{op_id, connector_id,
target_id}`` in :attr:`~meho_backplane.db.models.ApprovalRequest.proposed_effect`
— and because the original dispatch ``params`` are deliberately never
serialised onto a reviewer-facing surface (#1503), the four-eyes
approver could not tell a one-VM power cycle from a 1000-VM outage.
This wires all 8 write composites onto the per-op preview hook shipped
by #1437 (:mod:`meho_backplane.operations._preview`), following the
argocd pattern (#1452): reuse the handlers' own read-only resolution
helpers, never the mutating sub-ops.

========================  ====================================================
``vmware.composite.*``    preview stored in ``proposed_effect["preview"]``
========================  ====================================================
``vm.power.bulk``         ``{action, filter, resolved, total_resolved}``
``host.evacuate``         ``{host, tolerate_partial_failure, resolved,
                          total_resolved}``
``host.detach_from_vds``  ``{host, dvs, fallback_network, resolved,
                          total_resolved}``
``cluster.patch``         ``{cluster, patch_method, resolved, total_resolved}``
``vm.create``             echo: name, guest_os, sizing, networks, power-on
``vm.clone``              echo: source_vm, target_name, library_item, wait flag
``vm.snapshot.revert``    echo: vm, snapshot_name
``vm.migrate``            echo: vm, cluster, target_host + resolution source
========================  ====================================================

Two preview depths, chosen per composite
========================================

* **Live-read resolution** for the four fan-out composites whose blast
  radius is *not* derivable from params — a filter / host / cluster
  resolves to N entities only at vCenter. These call the same shared
  read-only helpers the handlers use at dispatch time
  (:func:`._write._resolve_vm_list` / :func:`._write._resolve_cluster_hosts`),
  so the reviewer sees the resolved entity list (capped at
  :data:`_PREVIEW_RESOLVED_CAP`, with ``total_resolved`` carrying the
  uncapped count).
* **Param echo** (no I/O — the ``secret.move`` precedent, #1580) for the
  four single-entity composites whose params fully name the blast
  radius. ``vm.migrate`` deliberately does **not** pre-resolve a DRS
  recommendation: DRS output is point-in-time and the approved dispatch
  re-consults it, so echoing a predicted host could mislead the reviewer
  — the preview says ``target_host_source="drs_at_execution"`` instead.

How preview reads execute
=========================

Post-#2256 the write handlers resolve their live-read helpers
(:func:`._write._resolve_vm_list` / :func:`._write._resolve_cluster_hosts`)
**directly on the connector session** rather than through
``dispatch_child``. The live-read preview builders reuse those same
helpers verbatim, passing the connector instance the dispatcher already
resolved into *ctx* (:attr:`PreviewContext.connector_instance`). Because
the helpers issue a plain ``GET`` on the connector session, the three
properties the old park-time ``dispatch_child`` adapter enforced hold
intrinsically: no policy-gate re-entry (a direct session call cannot
re-enter the dispatcher), no unparented audit rows (a direct read writes
none), and reads-only (the helpers only ever issue the listing ``GET``).
The k8s.apply dry-run (#1437), the argocd snapshot reads (#1452), and the
vault capability probe (#1504) run their preview I/O the same way —
connector-level, un-dispatched.

Redaction posture
=================

The whole-builder ``classify_op`` gate runs in
:func:`~meho_backplane.operations._preview.build_proposed_effect` before
any builder fires: the 8 op_ids classify as ``write`` (``.create`` /
``.patch`` suffixes) or ``other`` — none is a credential class, so none
is suppressed. The previews themselves carry only vSphere inventory
identity (moids, display names, power states) and the operator's own
dispatch params — infrastructure topology, never credential material.

Fail-soft: every builder either declines (``None`` → identifier-only
default) on malformed params or lets resolution faults propagate into
``build_proposed_effect``'s catch, which parks with the identifier
fields plus an explicit ``preview_unavailable`` marker + reason (#1628)
— the park always proceeds, and a reviewer can tell "blast-radius
unknown" from a genuinely small action.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from meho_backplane.connectors.vmware_rest.composites._write import (
    _resolve_cluster_hosts,
    _resolve_vm_list,
)
from meho_backplane.operations._preview import (
    PreviewBuilder,
    PreviewContext,
    register_preview_builder,
)

#: Cap on the ``resolved`` entity list stored in the durable
#: ``proposed_effect`` row. ``total_resolved`` always carries the uncapped
#: count, so a reviewer of a 1000-VM bulk op sees the first 20 entities
#: *and* the true blast-radius number without the row ballooning.
_PREVIEW_RESOLVED_CAP = 20


def _vm_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Identity-only projection of a VM listing row for the durable preview.

    Keeps moid + display name + power state and drops the rest (cpu /
    memory sizing) — the approval row needs to *name* the blast radius,
    not snapshot the inventory.
    """
    return {key: row[key] for key in ("vm", "name", "power_state") if row.get(key) is not None}


def _host_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Identity-only projection of a cluster-host listing row."""
    return {key: row[key] for key in ("host", "name") if row.get(key) is not None}


def _capped_resolution(
    rows: list[dict[str, Any]],
    project: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Render ``{resolved, total_resolved}`` with :data:`_PREVIEW_RESOLVED_CAP` applied."""
    return {
        "resolved": [project(row) for row in rows[:_PREVIEW_RESOLVED_CAP]],
        "total_resolved": len(rows),
    }


async def _vm_power_bulk_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.power.bulk`` — echo action + filter, resolve the matched VM set.

    Resolves the same filtered listing the handler's fan-out would act on
    via the shared :func:`._write._resolve_vm_list` (one read-only GET),
    so the reviewer sees how many — and which — VMs the approved power
    action would hit. The per-VM power sub-ops never fire here.
    """
    action = ctx.params.get("action")
    if not isinstance(action, str) or ctx.connector_instance is None:
        return None
    filter_dict: dict[str, Any] = dict(ctx.params.get("filter") or {})
    rows = await _resolve_vm_list(
        connector=ctx.connector_instance,  # type: ignore[arg-type]
        target=ctx.target,
        operator=ctx.operator,
        filter_dict=filter_dict,
    )
    return {
        "action": action,
        "filter": filter_dict,
        **_capped_resolution(rows, _vm_identity),
    }


async def _host_evacuate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``host.evacuate`` — resolve the VM set the evacuation would migrate.

    Same ``{"hosts": [host]}`` filter the handler uses; the recursive
    ``vm.migrate`` calls and the maintenance-enter never fire here.
    """
    host = ctx.params.get("host")
    if not isinstance(host, str) or ctx.connector_instance is None:
        return None
    rows = await _resolve_vm_list(
        connector=ctx.connector_instance,  # type: ignore[arg-type]
        target=ctx.target,
        operator=ctx.operator,
        filter_dict={"hosts": [host]},
    )
    return {
        "host": host,
        "tolerate_partial_failure": bool(ctx.params.get("tolerate_partial_failure", False)),
        **_capped_resolution(rows, _vm_identity),
    }


async def _host_detach_from_vds_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``host.detach_from_vds`` — resolve the VMs whose NICs would migrate.

    Echoes the detach coordinates and resolves the host's VM set (the
    entities whose NICs move to ``fallback_network`` before the DVS
    detach). The NIC PATCHes and the DVS remove_host never fire here.
    """
    host = ctx.params.get("host")
    dvs = ctx.params.get("dvs")
    fallback_network = ctx.params.get("fallback_network")
    if not isinstance(host, str) or not isinstance(dvs, str) or ctx.connector_instance is None:
        return None
    rows = await _resolve_vm_list(
        connector=ctx.connector_instance,  # type: ignore[arg-type]
        target=ctx.target,
        operator=ctx.operator,
        filter_dict={"hosts": [host]},
    )
    return {
        "host": host,
        "dvs": dvs,
        "fallback_network": fallback_network,
        **_capped_resolution(rows, _vm_identity),
    }


async def _cluster_patch_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``cluster.patch`` — resolve the host set the rolling patch would walk.

    Uses the shared :func:`._write._resolve_cluster_hosts` listing; the
    per-host maintenance / patch sub-ops never fire here.
    """
    cluster = ctx.params.get("cluster")
    if not isinstance(cluster, str) or ctx.connector_instance is None:
        return None
    rows = await _resolve_cluster_hosts(
        connector=ctx.connector_instance,  # type: ignore[arg-type]
        target=ctx.target,
        operator=ctx.operator,
        cluster_moid=cluster,
    )
    return {
        "cluster": cluster,
        "patch_method": ctx.params.get("patch_method", "default"),
        **_capped_resolution(rows, _host_identity),
    }


async def _vm_create_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.create`` — echo the creation spec (no I/O).

    The params fully name what gets created; mirroring the handler's
    defaults makes the echoed sizing the sizing the approved dispatch
    will use.
    """
    name = ctx.params.get("name")
    guest_os = ctx.params.get("guest_os")
    if not isinstance(name, str) or not isinstance(guest_os, str):
        return None
    nics = ctx.params.get("nics") or []
    networks = [nic.get("network") for nic in nics if isinstance(nic, dict) and nic.get("network")]
    return {
        "name": name,
        "guest_os": guest_os,
        "folder_name": ctx.params.get("folder_name"),
        "cpu_count": int(ctx.params.get("cpu_count", 1)),
        "memory_mib": int(ctx.params.get("memory_mib", 1024)),
        "networks": networks,
        "power_on_after_create": bool(ctx.params.get("power_on_after_create", False)),
    }


async def _vm_clone_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.clone`` — echo the clone coordinates (no I/O)."""
    source_vm = ctx.params.get("source_vm")
    target_name = ctx.params.get("target_name")
    library_item = ctx.params.get("library_item")
    if (
        not isinstance(source_vm, str)
        or not isinstance(target_name, str)
        or not isinstance(library_item, str)
    ):
        return None
    return {
        "source_vm": source_vm,
        "target_name": target_name,
        "library_item": library_item,
        "wait_for_completion": bool(ctx.params.get("wait_for_completion", True)),
    }


async def _vm_snapshot_revert_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.snapshot.revert`` — echo the revert coordinates (no I/O).

    The destructive scope is "this VM reverts to the snapshot named X",
    which the params fully convey; match/ambiguity resolution stays the
    handler's job (it refuses ambiguous or missing names safely).
    """
    vm = ctx.params.get("vm")
    snapshot_name = ctx.params.get("snapshot_name")
    if not isinstance(vm, str) or not isinstance(snapshot_name, str):
        return None
    return {"vm": vm, "snapshot_name": snapshot_name}


async def _vm_migrate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vm.migrate`` — echo coordinates + how the target host resolves.

    Deliberately no DRS read: a recommendation fetched at park time is
    point-in-time and the approved dispatch re-consults DRS, so echoing a
    predicted host could mislead the reviewer. ``target_host_source``
    says whether the host is operator-pinned or DRS-resolved at
    execution.
    """
    vm = ctx.params.get("vm")
    cluster = ctx.params.get("cluster")
    if not isinstance(vm, str) or not isinstance(cluster, str):
        return None
    explicit_target = ctx.params.get("target_host")
    if isinstance(explicit_target, str):
        return {
            "vm": vm,
            "cluster": cluster,
            "target_host": explicit_target,
            "target_host_source": "operator",
        }
    return {
        "vm": vm,
        "cluster": cluster,
        "target_host": None,
        "target_host_source": "drs_at_execution",
    }


#: op_id → builder for the 8 write composites. Module-level so the
#: registration below and the wiring tests share one source of truth.
_WRITE_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {
    "vmware.composite.vm.create": _vm_create_preview,
    "vmware.composite.vm.clone": _vm_clone_preview,
    "vmware.composite.vm.snapshot.revert": _vm_snapshot_revert_preview,
    "vmware.composite.vm.migrate": _vm_migrate_preview,
    "vmware.composite.vm.power.bulk": _vm_power_bulk_preview,
    "vmware.composite.host.evacuate": _host_evacuate_preview,
    "vmware.composite.host.detach_from_vds": _host_detach_from_vds_preview,
    "vmware.composite.cluster.patch": _cluster_patch_preview,
}


def _register_vmware_write_preview_builders() -> None:
    """Wire the 8 write-composite park-time preview builders. Import-time.

    The 5 read composites register no builder — they are
    ``requires_approval=False`` and never park, so a preview would be
    dead code.
    """
    for op_id, builder in _WRITE_PREVIEW_BUILDERS.items():
        register_preview_builder(op_id, builder)


_register_vmware_write_preview_builders()
