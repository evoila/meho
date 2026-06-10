# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_vmware_composite_operations`` -- registrar for the 13 composites.

Module-level async function called from the lifespan-driven
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
after the registrar list is populated by the
``meho_backplane.connectors.vmware_rest.composites`` package's
``__init__`` (which appends this function via
:func:`register_typed_op_registrar`).

Per-composite arguments (summary / description / group_key / tags /
``parameter_schema`` / ``safety_level`` / ``requires_approval``) live
here so a future shape change (e.g. ``llm_instructions`` polish) only
touches one file. The
:func:`~meho_backplane.operations.typed_register.register_composite_operation`
helper handles the upsert, body-hash dedupe, embedding pipeline, and
the source_kind="composite" persistence.

Mixed safety posture
--------------------

The 5 read composites (T5 / #508) pass ``safety_level="safe"`` +
``requires_approval=False`` -- overrides of T4's ``dangerous`` /
``True`` defaults. The 8 write composites (T6 / #509) inherit the T4
defaults explicitly (pass ``"dangerous"`` / ``True`` for clarity at
the call site; the helper would default to those values anyway).
Each :class:`_CompositeSpec` row carries its own ``safety_level`` +
``requires_approval`` so the policy posture is implied by the row,
not by global state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_patch_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_create_composite,
    vm_migrate_composite,
    vm_power_bulk_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
    CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA,
    CLUSTER_PATCH_PARAMETER_SCHEMA,
    CLUSTER_PATCH_RESPONSE_SCHEMA,
    DATASTORE_USAGE_PARAMETER_SCHEMA,
    DATASTORE_USAGE_RESPONSE_SCHEMA,
    EVENT_TAIL_PARAMETER_SCHEMA,
    EVENT_TAIL_RESPONSE_SCHEMA,
    HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
    HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
    HOST_EVACUATE_PARAMETER_SCHEMA,
    HOST_EVACUATE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
    PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
    PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
    VM_CLONE_PARAMETER_SCHEMA,
    VM_CLONE_RESPONSE_SCHEMA,
    VM_CREATE_PARAMETER_SCHEMA,
    VM_CREATE_RESPONSE_SCHEMA,
    VM_MIGRATE_PARAMETER_SCHEMA,
    VM_MIGRATE_RESPONSE_SCHEMA,
    VM_POWER_BULK_PARAMETER_SCHEMA,
    VM_POWER_BULK_RESPONSE_SCHEMA,
    VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
    VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA,
)
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_vmware_composite_operations"]


# Natural-key shorthand. Every composite registers against
# ``(product="vmware", version="9.0", impl_id="vmware-rest")`` -- the
# same triple :class:`VmwareRestConnector` advertises -- so the
# dispatcher's ``connector_id="vmware-rest-9.0"`` lookup resolves
# every read composite alongside the ~3,470 ingested ops.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"


#: Curated agent-actionable group selectors for the vmware-rest
#: composite surface (T4b #732). Surfaced verbatim by
#: ``list_operation_groups`` so the LLM client picks the right
#: composite group before drilling into ``search_operations``. Each
#: string differentiates against the other six composite groups *and*
#: against the ~3,470 ingested raw-REST ops that share the same
#: ``(vmware, 9.0, vmware-rest)`` connector key -- a curated composite
#: is the right route when one operator question maps to N raw REST
#: calls plus rollback / polling / aggregation logic.
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "cluster": (
        "Use for cluster-level reads and orchestrated cluster ops "
        "that aggregate across hosts: DRS state + active "
        "recommendations (read), and sequential cluster patch (write, "
        "approval-gated). The right group when the question is "
        "'what is DRS suggesting?' or 'patch every host in this "
        "cluster in order'. Pair with the 'host' group when the "
        "follow-up drills into one host's lifecycle (evacuate, "
        "maintenance), and with 'vm' when DRS recommendations need "
        "to translate into actual VM migrations."
    ),
    "events": (
        "Use for vCenter event-stream questions: 'what changed in "
        "the last N events?' tail via EventManager.QueryEvents. "
        "Read-only. The right group for live incident triage when "
        "the operator doesn't yet know which entity to drill into. "
        "Pair with 'vm' or 'host' once the event names a target "
        "moid to inspect."
    ),
    "performance": (
        "Use for performance-counter inspection on a single entity "
        "(VM, host, cluster, datastore): discover available counters "
        "via QueryAvailablePerfMetric, sample values via QueryPerf, "
        "return both in one call. Read-only. The right group for "
        "'is this VM hot?' / 'what does the last hour of CPU look "
        "like?' questions. Pair with 'vm' / 'host' to convert "
        "moids the operator already knows into one-shot perf "
        "snapshots."
    ),
    "storage": (
        "Use for datastore usage and VM-to-datastore placement: "
        "capacity / free space / type per datastore plus the "
        "vm_count + vm_names enrichment via the placement filter. "
        "Read-only. The right group for 'where is this VM stored?', "
        "'which datastores are running low?', or 'how many VMs live "
        "on this datastore?'. Pair with 'vm' when the question moves "
        "from 'which datastore?' to acting on a specific VM."
    ),
    "networking": (
        "Use for distributed-switch and portgroup audits: enumerate "
        "DVS + portgroups, then enrich each portgroup with parent "
        "DVS and connected VM names. Read-only. The right group for "
        "'what's connected to this portgroup?' / 'which DVS does "
        "this VM live on?' questions, and a prerequisite read before "
        "the 'host' group's host_detach_from_vds composite write. "
        "Pair with 'vm' for the post-audit drill-in into one VM's "
        "NICs."
    ),
    "vm": (
        "Use for VM-lifecycle write composites: create with NIC "
        "attach + optional power-on (rollback on partial failure), "
        "clone from a content-library template (long-running task "
        "polling), revert to a named snapshot (ambiguity-rejecting), "
        "migrate via DRS or explicit host, bulk power across a "
        "filter. Every op is dangerous / approval-required. The "
        "right group for any operator workflow that would otherwise "
        "be a ``govc vm.*`` invocation orchestrating multiple raw "
        "REST calls. Pair with 'storage' / 'networking' / 'cluster' "
        "for the pre-flight reads that shape the create / migrate "
        "parameters."
    ),
    "host": (
        "Use for host-lifecycle write composites: evacuate every "
        "VM off a host (recursive composite call into vm.migrate) "
        "then enter maintenance, or detach a host from a DVS after "
        "migrating its VM NICs off. Dangerous / approval-required; "
        "the host_evacuate composite is the first production "
        "composite that calls another composite. The right group "
        "for 'safely take this host offline' workflows. Pair with "
        "'networking' for the DVS-audit prerequisite to "
        "host_detach_from_vds, and with 'cluster' / 'vm' for the "
        "pre-flight reads."
    ),
}


class _CompositeSpec(NamedTuple):
    """Per-composite registration arguments.

    Field-table form rather than thirteen repeated kwargs blocks:
    keeps the op_id / handler / schemas / group / tags / policy
    posture adjacent per composite and drops the outer registrar
    function below the 100-line block limit. Common fields
    (``product`` / ``version`` / ``impl_id``) live on the call site
    below, not in the spec.

    Each row carries its own ``safety_level`` + ``requires_approval``
    so the policy posture is implied by the spec, not by global
    defaults: reads ship ``"safe"`` / ``False``; writes ship
    ``"dangerous"`` / ``True``.
    """

    op_id: str
    handler: Callable[..., Awaitable[dict[str, Any]]]
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    group_key: str
    tags: list[str]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool


_COMPOSITES: tuple[_CompositeSpec, ...] = (
    # ----------------------------------------------------------------
    # Read composites (T5 / #508) -- safe / no approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_recommendations",
        handler=cluster_drs_recommendations_composite,
        summary="Read DRS state + active recommendations for a cluster.",
        description=(
            "Orchestrates a cluster summary read plus a DRS-config read, "
            "returning a single aggregated payload. Equivalent of "
            "'govc cluster.recommendations' for the operator-facing "
            "workflow: one composite call replaces two raw vCenter REST "
            "GETs while preserving the audit-tree linkage between the "
            "parent composite row and each sub-op row. Read-only -- "
            "never mutates cluster state."
        ),
        parameter_schema=CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "read-only", "cluster", "drs"],
        safety_level="safe",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="vmware.composite.event.tail",
        handler=event_tail_composite,
        summary="Tail recent vCenter events via EventManager.QueryEvents.",
        description=(
            "Calls EventManager.QueryEvents (vi-json) against the "
            "EventManager singleton, optionally narrowed by a per-call "
            "moId override, and caps the returned array client-side. "
            "Equivalent of 'govc events' for the operator-facing "
            "workflow. Read-only -- never mutates the event store."
        ),
        parameter_schema=EVENT_TAIL_PARAMETER_SCHEMA,
        response_schema=EVENT_TAIL_RESPONSE_SCHEMA,
        group_key="events",
        tags=["composite", "read-only", "events", "vi-json"],
        safety_level="safe",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="vmware.composite.performance.summary",
        handler=performance_summary_composite,
        summary="Summarise performance metrics for one entity via PerformanceManager.",
        description=(
            "Discovers available counters for the target entity via "
            "PerformanceManager.QueryAvailablePerfMetric, then fetches "
            "sample values via PerformanceManager.QueryPerf (both "
            "vi-json). Returns the available-counter list plus the "
            "capped sample list; the caller can post-filter to whichever "
            "metric they need. Read-only -- never mutates counter "
            "configuration."
        ),
        parameter_schema=PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
        response_schema=PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
        group_key="performance",
        tags=["composite", "read-only", "performance", "vi-json"],
        safety_level="safe",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.usage",
        handler=datastore_usage_composite,
        summary="List datastores with capacity, free space, and VM placement.",
        description=(
            "Reads the datastore listing, then per-datastore detail "
            "(capacity, free space, type) plus the VM-placement filter "
            "via 'GET:/vcenter/vm?filter.datastores=...'. Aggregates "
            "into one row per datastore including vm_count + vm_names. "
            "Equivalent of an operator-facing 'storage usage report' "
            "that would otherwise require 1 + N sub-calls. Read-only -- "
            "never mutates storage state."
        ),
        parameter_schema=DATASTORE_USAGE_PARAMETER_SCHEMA,
        response_schema=DATASTORE_USAGE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore"],
        safety_level="safe",
        requires_approval=False,
    ),
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.audit",
        handler=network_portgroup_audit_composite,
        summary="Audit distributed portgroups with parent DVS + connected VMs.",
        description=(
            "Reads the distributed-switches listing (for parent-DVS name "
            "enrichment) plus the distributed portgroups via "
            "'GET:/vcenter/network?filter.types=DISTRIBUTED_PORTGROUP', "
            "then per-portgroup queries the VM list via "
            "'GET:/vcenter/vm?filter.networks=...'. Aggregates one row "
            "per portgroup with its parent DVS + connected VM names. "
            "Equivalent of 'govc dvs.portgroup.info' rolled up across "
            "every portgroup. Read-only -- never mutates network "
            "configuration."
        ),
        parameter_schema=NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "read-only", "networking", "portgroup"],
        safety_level="safe",
        requires_approval=False,
    ),
    # ----------------------------------------------------------------
    # Write composites (T6 / #509) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.create",
        handler=vm_create_composite,
        summary="Create a VM with NIC attach + optional power-on; rollback on failure.",
        description=(
            "Orchestrates folder lookup, POST:/vcenter/vm create, per-NIC "
            "attach via PATCH:/vcenter/vm/{vm}/network, and optional "
            "POST:/vcenter/vm/{vm}/power start. Partial-failure rollback: "
            "if any step after the create succeeds fails, the half-"
            "created VM is removed via DELETE:/vcenter/vm/{vm} so the "
            "caller knows the VM did not persist. Equivalent of 'govc "
            "vm.create' for operator-facing dispatch."
        ),
        parameter_schema=VM_CREATE_PARAMETER_SCHEMA,
        response_schema=VM_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.clone",
        handler=vm_clone_composite,
        summary="Clone a VM from a content-library template; poll the deploy task.",
        description=(
            "Reads source VM config, dispatches "
            "POST:/vcenter/vm-template/library-items?action=deploy, then "
            "polls GET:/cis/tasks/{task} until completion or timeout. "
            "Long-running -- blocks for up to timeout_seconds when "
            "wait_for_completion=True (default). Setting "
            "wait_for_completion=False returns the task id for caller "
            "polling. Equivalent of 'govc vm.clone' for operator-facing "
            "dispatch."
        ),
        parameter_schema=VM_CLONE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "long-running"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.snapshot.revert",
        handler=vm_snapshot_revert_composite,
        summary="Revert a VM to a named snapshot; reject on name ambiguity.",
        description=(
            "Lists the VM's snapshot tree via "
            "GET:/vcenter/vm/{vm}/snapshot, matches by snapshot name, "
            "and dispatches "
            "POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert when "
            "exactly one match is found. Multiple-match cases return "
            "status='ambiguous' with candidates listed so the operator "
            "can re-dispatch by snapshot moid. Idempotent within a "
            "snapshot tree -- reverting twice to the same snapshot is "
            "a no-op vs. vSphere state. Marked dangerous: the revert "
            "destroys in-flight VM state since the snapshot. Equivalent "
            "of 'govc snapshot.revert'."
        ),
        parameter_schema=VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.migrate",
        handler=vm_migrate_composite,
        summary="Migrate a VM via DRS recommendation or explicit target host.",
        description=(
            "Consults "
            "GET:/vcenter/cluster/{cluster}/drs/recommendations for the "
            "VM, then dispatches POST:/vcenter/vm/{vm}?action=relocate "
            "with the recommended host. If DRS returns no recommendation "
            "and no target_host override is supplied, the composite "
            "returns status='no_recommendation' rather than picking a "
            "host arbitrarily. The operator can bypass DRS by passing "
            "target_host explicitly. Equivalent of 'govc vm.migrate' "
            "for operator-facing dispatch."
        ),
        parameter_schema=VM_MIGRATE_PARAMETER_SCHEMA,
        response_schema=VM_MIGRATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "drs"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.power.bulk",
        handler=vm_power_bulk_composite,
        summary="Apply a power action to every VM matching a filter; aggregate results.",
        description=(
            "Resolves a free-form filter to a VM list via "
            "GET:/vcenter/vm, then dispatches "
            "POST:/vcenter/vm/{vm}/power?action=<action> per matched VM. "
            "Partial-failure tolerated: each VM's outcome is captured "
            "independently; failures do not abort the composite unless "
            "fail_fast=True. Returns per-VM results plus aggregate "
            "counts. Equivalent of 'govc vm.power' over a --vm glob."
        ),
        parameter_schema=VM_POWER_BULK_PARAMETER_SCHEMA,
        response_schema=VM_POWER_BULK_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "bulk"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.evacuate",
        handler=host_evacuate_composite,
        summary="Migrate every VM off a host (via recursive vm.migrate) then enter maintenance.",
        description=(
            "Lists VMs on the host via "
            "GET:/vcenter/vm?filter.hosts=..., then dispatches "
            "vmware.composite.vm.migrate per VM (recursive composite "
            "call -- first production composite that calls another "
            "composite). On full migration success, the host enters "
            "maintenance via "
            "PATCH:/vcenter/host/{host}/maintenance?action=enter. "
            "tolerate_partial_failure=True lets maintenance-enter fire "
            "even with VMs left behind. Equivalent of 'govc host.evacuate' "
            "operator workflow."
        ),
        parameter_schema=HOST_EVACUATE_PARAMETER_SCHEMA,
        response_schema=HOST_EVACUATE_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "maintenance", "recursive"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.detach_from_vds",
        handler=host_detach_from_vds_composite,
        summary="Migrate host VM NICs off a DVS to a fallback network, then remove host from DVS.",
        description=(
            "Lists DVS portgroups on the host and VMs on the host, "
            "migrates each VM's NICs off the DVS to the supplied "
            "fallback_network via PATCH:/vcenter/vm/{vm}/network, and "
            "then dispatches "
            "POST:/vcenter/network/dvs/{dvs}?action=remove_host. "
            "vSphere refuses the host detach when any VM still has "
            "active NICs on the DVS -- the composite verifies every NIC "
            "migrated before attempting the detach; on partial NIC "
            "migration the composite returns status='incomplete' and "
            "skips the DVS detach. Replaces "
            "scripts/host-detach-from-vds.py."
        ),
        parameter_schema=HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
        response_schema=HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "networking"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.patch",
        handler=cluster_patch_composite,
        summary="Sequentially patch every host in a cluster: maintenance + patch + exit.",
        description=(
            "Lists cluster hosts via GET:/vcenter/cluster/{cluster}/host, "
            "then iterates each host sequentially: "
            "PATCH:/vcenter/host/{host}/maintenance?action=enter -> "
            "POST:/vcenter/host/{host}?action=patch -> "
            "PATCH:/vcenter/host/{host}/maintenance?action=exit. "
            "Sequential by design -- concurrent host patches would "
            "force every VM in the cluster to vMotion at once, "
            "overwhelming DRS. Per-host failure stops the loop; the "
            "composite returns status='stopped' with patched_hosts + "
            "remaining_hosts so the operator can manually finish or "
            "roll back the partial patch."
        ),
        parameter_schema=CLUSTER_PATCH_PARAMETER_SCHEMA,
        response_schema=CLUSTER_PATCH_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "patch", "long-running"],
        safety_level="dangerous",
        requires_approval=True,
    ),
)


async def register_vmware_composite_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every vmware-rest composite into ``endpoint_descriptor``.

    Idempotent: a second invocation against unchanged descriptions is a
    no-op for the embedding pipeline (the body-hash skip path in
    :func:`_register_in_session`). The runner
    (:func:`run_typed_op_registrars`) calls every registered registrar
    on every lifespan startup; the skip-re-embed branch keeps that
    cheap.

    Scope: 13 composites total -- 5 read (T5 / #508) +
    8 write (T6 / #509). Each composite's ``safety_level`` +
    ``requires_approval`` come from its :class:`_CompositeSpec` row:
    reads pass ``"safe"`` / ``False``; writes pass ``"dangerous"`` /
    ``True`` (T4's defaults).

    Test seam: ``embedding_service`` lets test fixtures inject a stub
    so unit tests don't load the ONNX model. Production callers leave
    it ``None`` and each registration resolves the process-wide
    singleton.
    """
    for spec in _COMPOSITES:
        await register_composite_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=spec.op_id,
            handler=spec.handler,
            summary=spec.summary,
            description=spec.description,
            parameter_schema=spec.parameter_schema,
            response_schema=spec.response_schema,
            group_key=spec.group_key,
            when_to_use=_WHEN_TO_USE_BY_GROUP[spec.group_key],
            tags=spec.tags,
            safety_level=spec.safety_level,
            requires_approval=spec.requires_approval,
            embedding_service=embedding_service,
        )
