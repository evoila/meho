# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_vmware_composite_operations`` -- registrar for the 24 composites.

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

The 5 read composites (T5 / #508) pass
``safety_level="safe"`` + ``requires_approval=False`` -- overrides of
T4's ``dangerous`` / ``True`` defaults. (The former
``host.network_uplinks`` and ``host.vsan_health`` reads were re-shipped
as typed ops in #2258; see
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops`.) The 19 write
composites (T6 / #509, single-VM ``vm.power`` / #2301, the mutating
VI-JSON ``vm.disk.grow`` / #2893, the folder-template
``vm.clone_from_template`` / #2894, the vim cluster / inventory writes
``cluster.drs_rule.create`` + ``folder.create`` / #2895, the #2891
hardware writes -- ``vm.resize`` / ``vm.nic.repoint`` /
``vm.device.cdrom``, the two GOSC composites
``guest.customization_spec.create`` / ``vm.customize`` / #2892, and the
OVF/OVA content-library deploy ``vm.deploy_from_library`` / #2909) inherit
the T4 defaults explicitly (pass ``"dangerous"`` / ``True`` for clarity
at the call site; the helper would default to those values anyway).
Each :class:`_CompositeSpec` row carries its own ``safety_level`` +
``requires_approval`` so the policy posture is implied by the row,
not by global state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_drs_rule_create_composite,
    cluster_patch_composite,
    folder_create_composite,
    guest_customization_spec_create_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_customize_composite,
    vm_deploy_from_library_composite,
    vm_device_cdrom_composite,
    vm_disk_grow_composite,
    vm_migrate_composite,
    vm_nic_repoint_composite,
    vm_power_bulk_composite,
    vm_power_composite,
    vm_resize_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
    CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA,
    CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
    CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA,
    CLUSTER_PATCH_PARAMETER_SCHEMA,
    CLUSTER_PATCH_RESPONSE_SCHEMA,
    DATASTORE_USAGE_PARAMETER_SCHEMA,
    DATASTORE_USAGE_RESPONSE_SCHEMA,
    EVENT_TAIL_PARAMETER_SCHEMA,
    EVENT_TAIL_RESPONSE_SCHEMA,
    FOLDER_CREATE_PARAMETER_SCHEMA,
    FOLDER_CREATE_RESPONSE_SCHEMA,
    GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA,
    GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA,
    HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
    HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
    HOST_EVACUATE_PARAMETER_SCHEMA,
    HOST_EVACUATE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
    PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
    PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
    VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA,
    VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA,
    VM_CLONE_PARAMETER_SCHEMA,
    VM_CLONE_RESPONSE_SCHEMA,
    VM_CREATE_PARAMETER_SCHEMA,
    VM_CREATE_RESPONSE_SCHEMA,
    VM_CUSTOMIZE_PARAMETER_SCHEMA,
    VM_CUSTOMIZE_RESPONSE_SCHEMA,
    VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA,
    VM_DEPLOY_FROM_LIBRARY_RESPONSE_SCHEMA,
    VM_DEVICE_CDROM_PARAMETER_SCHEMA,
    VM_DEVICE_CDROM_RESPONSE_SCHEMA,
    VM_DISK_GROW_PARAMETER_SCHEMA,
    VM_DISK_GROW_RESPONSE_SCHEMA,
    VM_MIGRATE_PARAMETER_SCHEMA,
    VM_MIGRATE_RESPONSE_SCHEMA,
    VM_NIC_REPOINT_PARAMETER_SCHEMA,
    VM_NIC_REPOINT_RESPONSE_SCHEMA,
    VM_POWER_BULK_PARAMETER_SCHEMA,
    VM_POWER_BULK_RESPONSE_SCHEMA,
    VM_POWER_PARAMETER_SCHEMA,
    VM_POWER_RESPONSE_SCHEMA,
    VM_RESIZE_PARAMETER_SCHEMA,
    VM_RESIZE_RESPONSE_SCHEMA,
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
        "polling) or from a folder VM template (CloneVM_Task, with "
        "optional inline guest customization), deploy an OVF/OVA "
        "content-library item to a new VM (deploy_from_library — "
        "OVF-network→portgroup mappings, ambiguity-rejecting name "
        "lookup, structured deploy-report statuses), revert to a named "
        "snapshot (ambiguity-rejecting), "
        "migrate via DRS or explicit host, bulk power across a "
        "filter, or a single-VM power verb (on/off/reset plus a "
        "Tools-mediated guest_shutdown/guest_reboot for one-off "
        "incident actions). Every op is dangerous / approval-required. The "
        "right group for any operator workflow that would otherwise "
        "be a ``govc vm.*`` invocation orchestrating multiple raw "
        "REST calls. Pair with 'storage' / 'networking' / 'cluster' "
        "for the pre-flight reads that shape the create / migrate "
        "parameters."
    ),
    "host": (
        "Use for host-lifecycle write "
        "composites. Write (dangerous / "
        "approval-required): evacuate "
        "every VM off a host (recursive composite call into "
        "vm.migrate) then enter maintenance, or detach a host from a "
        "DVS after migrating its VM NICs off; the host_evacuate "
        "composite is the first production composite that calls "
        "another composite. The right group for 'safely take this "
        "host offline' workflows. Pair with 'networking' for the "
        "DVS-audit prerequisite to host_detach_from_vds, and with "
        "'cluster' / 'vm' for the pre-flight reads."
    ),
    "guest": (
        "Use for guest OS customization (GOSC) write composites -- how a "
        "cloned VM gets its network identity on first boot. Write "
        "(dangerous / approval-required): create a reusable named "
        "customization spec (hostname + per-NIC static IP + gateway + "
        "DNS for Linux, or a Windows sysprep spec), and apply a saved "
        "spec to a powered-off VM (optional power-on afterward so it "
        "applies that boot). The right group after a 'vm' clone/create "
        "when the question is 'give this VM its hostname and static IP'. "
        "Pair with 'vm' for the clone/create that produces the VM, and "
        "with 'networking' when the per-NIC IPs need portgroup context. "
        "GOSC specs can carry admin / sysprep credentials; those never "
        "reach a reviewer surface (the create op is credential-class)."
    ),
}


class _CompositeSpec(NamedTuple):
    """Per-composite registration arguments.

    Field-table form rather than seventeen repeated kwargs blocks:
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
    # Read composites return a plain aggregation dict; migrated write
    # composites (#2256) may instead return an ``OperationResult`` verbatim
    # when the direct-session governance seam parks/denies an internal write,
    # so the handler contract widens to that union.
    handler: Callable[..., Awaitable[dict[str, Any] | OperationResult]]
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
            "workflow: one composite call replaces a raw vCenter REST "
            "GET plus a vim property read (DRS state is vim-only in "
            "vSphere 9.0). Read-only -- never mutates cluster state."
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
            "Lists the distributed portgroups via "
            "'GET:/vcenter/network?filter.types=DISTRIBUTED_PORTGROUP', "
            "then per-portgroup queries the VM list via "
            "'GET:/vcenter/vm?filter.networks=...'. Aggregates one row "
            "per portgroup with connected VM names. Parent-DVS name "
            "enrichment is degraded (dvs_name always null): the pinned "
            "spec serves no DVS list resource (#2970). Equivalent of "
            "'govc dvs.portgroup.info' rolled up across every "
            "portgroup. Read-only -- never mutates network "
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
        summary="Create a VM with NIC attach, optional VHV enable + optional power-on.",
        description=(
            "Orchestrates folder lookup, POST:/vcenter/vm create, per-NIC "
            "adapter create via POST:/vcenter/vm/{vm}/hardware/ethernet, "
            "an optional nested_hv vim ReconfigVM_Task (nestedHVEnabled, "
            "task-polled, applied before any power-on), and optional "
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
        summary="Clone a VM from a content-library template (synchronous deploy).",
        description=(
            "Reads source VM config, then dispatches "
            "POST:/vcenter/vm-template/library-items/{templateLibraryItem}"
            "?action=deploy. The pinned deploy operation is synchronous "
            "-- its 200 body is the deployed VM id, so the composite "
            "returns status='completed' with vm_id directly (no task "
            "poll). Equivalent of 'govc vm.clone' for operator-facing "
            "dispatch."
        ),
        parameter_schema=VM_CLONE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.deploy_from_library",
        handler=vm_deploy_from_library_composite,
        summary="Deploy an OVF/OVA content-library item to a new VM (retires govc library.deploy).",
        description=(
            "Deploys an OVF/OVA package from a content library to a new VM via "
            "the synchronous "
            "POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy. "
            "The library item is referenced by id (passthrough) or by name — "
            "resolved via POST:/content/library/item?action=find (filtered to "
            "type=ovf), optionally scoped by library name through "
            "POST:/content/library?action=find, with ambiguity refused "
            "(status='ambiguous_item' / 'ambiguous_library') before any deploy. "
            "resource_pool is the required placement anchor; host / folder / "
            "datastore refine it, and network_mappings maps each OVF network key "
            "to a portgroup. Unlike vm.clone (200 body is a bare VM id) the OVF "
            "deploy's 200 body is a DeploymentResult: a failed OVF / network / "
            "placement validation returns succeeded=false and surfaces as "
            "status='deploy_failed' with per-issue messages, and an invalid / "
            "missing placement resource (HTTP 400/404) as status='deploy_error' "
            "(a faulted content-library find during name resolution as "
            "status='resolve_error') — structured statuses, never a raw vendor "
            "error. With power_on the "
            "deployed VM is started best-effort. Equivalent of 'govc "
            "library.deploy' for operator-facing dispatch."
        ),
        parameter_schema=VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA,
        response_schema=VM_DEPLOY_FROM_LIBRARY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "ovf"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.snapshot.revert",
        handler=vm_snapshot_revert_composite,
        summary="Revert a VM to a named snapshot; reject on name ambiguity.",
        description=(
            "Reads the VM's snapshot tree (vim RetrievePropertiesEx on "
            "VirtualMachine.snapshot -- the pinned REST spec serves no "
            "snapshot resource, #2970), matches by snapshot name, and "
            "dispatches the vim "
            "VirtualMachineSnapshot.RevertToSnapshot_Task (polled to a "
            "terminal state) when "
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
            "Consults the cluster's DRS migration recommendations (vim "
            "RetrievePropertiesEx on "
            "ClusterComputeResource.drsRecommendation -- the pinned REST "
            "spec serves no DRS resource, #2970) for the "
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
        op_id="vmware.composite.vm.power",
        handler=vm_power_composite,
        summary="Apply one power verb to a single VM, including a Tools soft shutdown.",
        description=(
            "Single-VM power verb for one-off incident actions (the "
            "one-VM ergonomics vm.power.bulk's fan-out is clumsy for). "
            "Hard verbs on / off / reset hit "
            "POST:/vcenter/vm/{vm}/power?action=start|stop|reset; the two "
            "soft verbs guest_shutdown / guest_reboot hit "
            "POST:/vcenter/vm/{vm}/guest/power?action=shutdown|reboot for a "
            "clean Tools-mediated transition. A soft verb against a VM whose "
            "VMware Tools are not running fails typed "
            "(status='tools_unavailable', echoing the Tools state) rather "
            "than hanging, so the operator can fall back to a hard off. "
            "Equivalent of a single 'govc vm.power' invocation."
        ),
        parameter_schema=VM_POWER_PARAMETER_SCHEMA,
        response_schema=VM_POWER_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "power"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.disk.grow",
        handler=vm_disk_grow_composite,
        summary="Grow a VM's virtual disk to a larger capacity via ReconfigVM_Task.",
        description=(
            "Grows one virtual disk's capacity. The pinned 9.0 REST spec's "
            "Disk.UpdateSpec carries only backing (no capacity field), so the "
            "capacity change goes through vim VirtualMachine.ReconfigVM_Task — "
            "a single-device edit raising the VirtualDisk's capacityInBytes — "
            "the connector's first mutating VI-JSON call. Reads the VM's "
            "config.hardware.device to obtain the full VirtualDisk device + its "
            "current capacity, refuses a shrink (status='invalid_shrink') "
            "before any write, issues the ReconfigVM_Task edit through the same "
            "governance seam as the REST write composites, and polls the "
            "returned Task to a terminal state before reporting status='grown'. "
            "Grow-only by contract. Equivalent of 'govc vm.disk.change -size'."
        ),
        parameter_schema=VM_DISK_GROW_PARAMETER_SCHEMA,
        response_schema=VM_DISK_GROW_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "disk", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.clone_from_template",
        handler=vm_clone_from_template_composite,
        summary="Clone a folder VM template via CloneVM_Task, with optional inline GOSC.",
        description=(
            "Clones a folder VM template (a marked-as-template VM) via vim "
            "VirtualMachine.CloneVM_Task — the path vm.clone (content-library "
            "only) cannot serve. Resolves source_template by name "
            "(GET:/vcenter/vm?filter.names) and asserts config.template "
            "(PropertyCollector) before any clone, refusing a non-template "
            "source with status='not_a_template'. Builds the CloneSpec "
            "placement (folder / resource pool / datastore, optional host), "
            "optionally resolves customization_spec_name to an inline "
            "CustomizationSpec via CustomizationSpecManager.GetCustomizationSpec "
            "(composing with the GOSC surface so the clone customizes in one "
            "dispatch), issues CloneVM_Task through the same governance seam as "
            "the REST write composites, and polls the returned Task to a "
            "terminal state before reporting status='cloned'. The connector's "
            "folder-template deploy path — what govc/terraform use — and the "
            "only clone that supports inline customization at clone time."
        ),
        parameter_schema=VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "vi-json"],
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
            "maintenance via the vim "
            "HostSystem.EnterMaintenanceMode_Task (polled to a terminal "
            "state -- the pinned REST spec serves no host-maintenance "
            "path, #2970). "
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
            "migrates each VM's NICs to the supplied fallback_network "
            "per adapter (GET + "
            "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}), and then "
            "removes the host via the vim "
            "DistributedVirtualSwitch.ReconfigureDvs_Task host-member "
            "remove (polled to a terminal state -- the pinned REST spec "
            "serves no DVS write path, #2970). "
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
            "Lists cluster hosts via GET:/vcenter/host?clusters=..., "
            "then iterates each host sequentially: vim "
            "EnterMaintenanceMode_Task -> vLCM "
            "POST:/esx/settings/hosts/{host}/software?action=apply"
            "&vmw-task=true (cis task polled to terminal) -> vim "
            "ExitMaintenanceMode_Task, every task polled before the "
            "next step. "
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
    # ----------------------------------------------------------------
    # vim cluster / inventory writes (#2895) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_rule.create",
        handler=cluster_drs_rule_create_composite,
        summary="Add a DRS affinity / anti-affinity rule to a cluster by explicit VM list.",
        description=(
            "Adds a classic DRS affinity ('keep these VMs together') or "
            "anti-affinity ('keep these VMs apart') rule by explicit VM "
            "list. No cluster-rules REST path exists and the tag-based "
            "compute-policies surface is semantically wrong (tag-scoped, not "
            "an explicit VM list), so the add goes through vim "
            "ClusterComputeResource.ReconfigureComputeResource_Task with a "
            "single-rule ClusterConfigSpecEx.rulesSpec delta (modify=true) "
            "and polls the returned task to a terminal state. Resolves the VM "
            "names to MoRefs scoped to the cluster; rule names are the "
            "idempotence key, so a duplicate returns status='rule_exists' "
            "before any write. Equivalent of 'govc cluster.rule.create'."
        ),
        parameter_schema=CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.folder.create",
        handler=folder_create_composite,
        summary="Create a VM folder under a named parent (synchronous vim CreateFolder).",
        description=(
            "Creates a VM folder under a named parent. /vcenter/folder is "
            "GET-only, so the create goes through vim Folder.CreateFolder — "
            "which is synchronous: it returns the new folder's "
            "ManagedObjectReference directly (no task poll). Resolves the "
            "parent by display name among the VIRTUAL_MACHINE folders; an "
            "unknown name returns status='parent_not_found' and an ambiguous "
            "one status='ambiguous_parent', both before any write. Equivalent "
            "of 'govc folder.create' for the VM-folder inventory tree."
        ),
        parameter_schema=FOLDER_CREATE_PARAMETER_SCHEMA,
        response_schema=FOLDER_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "inventory", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    # ----------------------------------------------------------------
    # Hardware write composites (#2891) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.resize",
        handler=vm_resize_composite,
        summary="Reconfigure a VM's CPU count / cores-per-socket and/or memory.",
        description=(
            "Reads current sizing + hot-add flags via GET:/vcenter/vm/{vm}, "
            "then PATCHes PATCH:/vcenter/vm/{vm}/hardware/cpu and/or "
            "PATCH:/vcenter/vm/{vm}/hardware/memory. A freshly-cloned VM is "
            "stuck at the template's sizing; this rightsizes it. A change a "
            "powered-on VM cannot take live (no hot-add, a decrease, or a "
            "cores_per_socket change) returns status='requires_power_off' "
            "rather than a raw vCenter 400; a request already matching "
            "current returns 'no_change'. Equivalent of the sizing half of "
            "'govc vm.change'."
        ),
        parameter_schema=VM_RESIZE_PARAMETER_SCHEMA,
        response_schema=VM_RESIZE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.nic.repoint",
        handler=vm_nic_repoint_composite,
        summary="Repoint a vNIC to a different distributed portgroup.",
        description=(
            "Reads the NIC's current backing + MAC via "
            "GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}, resolves the "
            "target portgroup by display name via "
            "GET:/vcenter/network?filter.types=DISTRIBUTED_PORTGROUP (there "
            "is no dedicated portgroup list resource), then PATCHes the NIC "
            "backing to {type: DISTRIBUTED_PORTGROUP, network}. A name that "
            "resolves to zero / many portgroups refuses the repoint "
            "(status='not_found' / 'ambiguous') with no PATCH issued. The "
            "from->to network pair is what the four-eyes reviewer needs. "
            "Equivalent of 'govc vm.network.change'."
        ),
        parameter_schema=VM_NIC_REPOINT_PARAMETER_SCHEMA,
        response_schema=VM_NIC_REPOINT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "networking"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.device.cdrom",
        handler=vm_device_cdrom_composite,
        summary="Remove / update / disconnect a VM CD-ROM device.",
        description=(
            "Reads the CD-ROM's current backing + state via "
            "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom} (the host-local ISO "
            "path the approver needs to see), then dispatches the requested "
            "action: 'remove' (DELETE the device), 'update' (PATCH its "
            "backing, e.g. to CLIENT_DEVICE to un-pin a host-local ISO), or "
            "'disconnect' (POST ?action=disconnect). A template shipping a "
            "host-local-ISO-backed CD-ROM silently pins every clone to one "
            "host and blocks vMotion; this clears it. Equivalent of "
            "'govc device.cdrom.eject' / 'govc device.remove'."
        ),
        parameter_schema=VM_DEVICE_CDROM_PARAMETER_SCHEMA,
        response_schema=VM_DEVICE_CDROM_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    # ----------------------------------------------------------------
    # Guest customization (GOSC) composites (#2892) -- dangerous / approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.guest.customization_spec.create",
        handler=guest_customization_spec_create_composite,
        summary="Create a reusable named guest customization (GOSC) spec.",
        description=(
            "Creates a named GuestOS customization spec via "
            "POST:/vcenter/guest/customization-specs from the tractable "
            "provisioning subset: hostname (FIXED HostnameGenerator), "
            "per-NIC static IP / prefix / gateways, and global DNS, for "
            "either a Linux (linux_config) or Windows "
            "(windows_config sysprep) guest. The spec a later "
            "vmware.composite.vm.customize -- or a clone's "
            "guest_customization_spec -- references by name so a cloned "
            "VM comes up with its hostname and network identity. Windows "
            "admin / product-key / domain-join credentials are consumed "
            "into the sysprep body but never serialized onto any "
            "reviewer / preview / broadcast / audit surface (#1503): the "
            "op is credential-class and its park-time preview echoes "
            "identity fields only."
        ),
        parameter_schema=GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA,
        response_schema=GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA,
        group_key="guest",
        tags=["composite", "write", "guest", "customization", "provisioning"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.customize",
        handler=vm_customize_composite,
        summary="Apply a saved customization spec to a VM (by name); optional power-on.",
        description=(
            "Resolves a VM by display name via GET:/vcenter/vm, then "
            "applies a saved customization spec via "
            "PUT:/vcenter/vm/{vm}/guest/customization with the spec name. "
            "vCenter only accepts a pending customization on a "
            "powered-off VM, so the composite pre-checks the resolved "
            "power state and refuses a powered-on VM with "
            "status='precondition_failed' rather than letting the PUT "
            "400. Ambiguous / missing names return "
            "status='ambiguous' / 'not_found'. With power_on=True the VM "
            "is powered on afterward via "
            "POST:/vcenter/vm/{vm}/power?action=start so the "
            "customization applies on that boot. The spec reference "
            "carries no secret (the credential material lives in the "
            "saved spec)."
        ),
        parameter_schema=VM_CUSTOMIZE_PARAMETER_SCHEMA,
        response_schema=VM_CUSTOMIZE_RESPONSE_SCHEMA,
        group_key="guest",
        tags=["composite", "write", "guest", "vm", "customization"],
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

    Scope: 24 composites total -- 5 read (T5 / #508) + 19 write (T6 /
    #509, single-VM ``vm.power`` / #2301, the mutating VI-JSON
    ``vm.disk.grow`` / #2893, the folder-template
    ``vm.clone_from_template`` / #2894, the vim cluster / inventory writes
    ``cluster.drs_rule.create`` + ``folder.create`` / #2895, the #2891
    hardware writes ``vm.resize`` / ``vm.nic.repoint`` /
    ``vm.device.cdrom``, the two GOSC composites
    ``guest.customization_spec.create`` / ``vm.customize`` / #2892, and the
    OVF/OVA content-library deploy ``vm.deploy_from_library`` / #2909). (The
    former ``host.network_uplinks`` / ``host.vsan_health`` reads were
    re-shipped as typed ops in #2258.)
    Each composite's ``safety_level`` +
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
