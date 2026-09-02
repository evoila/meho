# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_vmware_composite_operations`` -- registrar for the 33 composites.

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

The 9 read composites (T5 / #508 + the 4 guest-ops reads
``vm.guest.process.list`` / ``vm.guest.env.read`` / ``vm.guest.net.show``
/ ``vm.guest.file.read`` / #3100) pass
``safety_level="safe"`` + ``requires_approval=False`` -- overrides of
T4's ``dangerous`` / ``True`` defaults. (The former
``host.network_uplinks`` and ``host.vsan_health`` reads were re-shipped
as typed ops in #2258; see
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops`.) 23 of the 24
write composites (T6 / #509, single-VM ``vm.power`` / #2301, the guest-ops
write ``vm.guest.file.write`` / #3100, the mutating
VI-JSON ``vm.disk.grow`` / #2893, the folder-template
``vm.clone_from_template`` / #2894, the vim cluster / inventory writes
``cluster.drs_rule.create`` + ``folder.create`` / #2895, the #2891
hardware writes -- ``vm.resize`` / ``vm.nic.repoint`` /
``vm.device.cdrom``, the two GOSC composites
``guest.customization_spec.create`` / ``vm.customize`` / #2892, the
OVF/OVA content-library deploy ``vm.deploy_from_library`` / #2909, and the
three host-domain writes ``host.datastore_mount_nfs`` /
``host.disk_mark_flash`` / ``host.service_control`` / #3182) inherit
the T4 defaults explicitly (pass ``"dangerous"`` / ``True`` for clarity
at the call site; the helper would default to those values anyway). The
24th write composite, ``vm.destroy`` / #3198, is the first
``safety_level="destructive"`` op (still ``requires_approval=True``) — the
governed-delete tier (decision
``docs/decisions/governed-delete-operations.md``).
Each :class:`_CompositeSpec` row carries its own ``safety_level`` +
``requires_approval`` so the policy posture is implied by the row,
not by global state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._guest import (
    guest_env_read_composite,
    guest_file_read_composite,
    guest_file_write_composite,
    guest_net_show_composite,
    guest_process_list_composite,
    guest_program_run_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host import (
    datastore_mount_nfs_composite,
    disk_mark_flash_composite,
    service_control_composite,
)
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
    network_portgroup_create_composite,
    network_portgroup_security_set_composite,
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_customize_composite,
    vm_deploy_from_library_composite,
    vm_destroy_composite,
    vm_device_cdrom_composite,
    vm_disk_attach_composite,
    vm_disk_grow_composite,
    vm_import_from_library_composite,
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
    GUEST_ENV_READ_PARAMETER_SCHEMA,
    GUEST_ENV_READ_RESPONSE_SCHEMA,
    GUEST_FILE_READ_PARAMETER_SCHEMA,
    GUEST_FILE_READ_RESPONSE_SCHEMA,
    GUEST_FILE_WRITE_PARAMETER_SCHEMA,
    GUEST_FILE_WRITE_RESPONSE_SCHEMA,
    GUEST_NET_SHOW_PARAMETER_SCHEMA,
    GUEST_NET_SHOW_RESPONSE_SCHEMA,
    GUEST_PROCESS_LIST_PARAMETER_SCHEMA,
    GUEST_PROCESS_LIST_RESPONSE_SCHEMA,
    GUEST_PROGRAM_RUN_PARAMETER_SCHEMA,
    GUEST_PROGRAM_RUN_RESPONSE_SCHEMA,
    HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA,
    HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA,
    HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
    HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
    HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA,
    HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA,
    HOST_EVACUATE_PARAMETER_SCHEMA,
    HOST_EVACUATE_RESPONSE_SCHEMA,
    HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
    HOST_SERVICE_CONTROL_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA,
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
    VM_DESTROY_PARAMETER_SCHEMA,
    VM_DESTROY_RESPONSE_SCHEMA,
    VM_DEVICE_CDROM_PARAMETER_SCHEMA,
    VM_DEVICE_CDROM_RESPONSE_SCHEMA,
    VM_DISK_ATTACH_PARAMETER_SCHEMA,
    VM_DISK_ATTACH_RESPONSE_SCHEMA,
    VM_DISK_GROW_PARAMETER_SCHEMA,
    VM_DISK_GROW_RESPONSE_SCHEMA,
    VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
    VM_IMPORT_FROM_LIBRARY_RESPONSE_SCHEMA,
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
        "Use for distributed-switch and portgroup work: audit "
        "(enumerate DVS + portgroups, enrich each with parent DVS + "
        "connected VM names, read-only) and the two portgroup writes "
        "for standing up an L2 substrate -- create a distributed "
        "portgroup on a DVS with a VLAN trunk (or access) spec, and "
        "set its security policy (promiscuous / forged-transmits / "
        "MAC-changes), both approval-gated. The right group for "
        "'what's connected to this portgroup?' / 'which DVS does "
        "this VM live on?' and 'create the trunk portgroup my nested "
        "ESXi attaches to'. The audit read surfaces the DVS / "
        "portgroup moids the two writes take. Pair with 'vm' for the "
        "post-audit drill-in into one VM's NICs, and with 'host' "
        "before its host_detach_from_vds write."
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
        "Use for host-lifecycle and host-domain write "
        "composites. Write (dangerous / "
        "approval-required): evacuate "
        "every VM off a host (recursive composite call into "
        "vm.migrate) then enter maintenance, or detach a host from a "
        "DVS after migrating its VM NICs off; the host_evacuate "
        "composite is the first production composite that calls "
        "another composite. Plus the host-domain writes (#3182), each "
        "vCenter-mediated through the governed VI-JSON seam: mount an "
        "NFS export as a datastore on a host "
        "(datastore_mount_nfs), mark host disks as flash/HDD for "
        "vSAN-ready validation (disk_mark_flash), or start/stop/restart "
        "a bounded host service and set its policy (service_control, "
        "allowlist-enforced — never an arbitrary service name). The "
        "host is selected by display name or moref. The right group for "
        "'safely take this host offline' and host bring-up / prep "
        "workflows. Pair with 'networking' for the "
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
    "guest_ops": (
        "Use for the governed guest-operations channel -- reaching INSIDE a "
        "running VM's guest OS via VMware Tools (vim GuestOperationsManager), "
        "the governed replacement for out-of-band 'govc guest.run'. Reads "
        "(safe): list guest processes (process.list -- exec status), read "
        "guest environment variables (env.read), show Tools-reported guest "
        "network state (net.show -- per-NIC IPs / routes / DNS, needs NO guest "
        "credentials), and initiate a guest file read (file.read -- returns the "
        "transfer handle: size + attributes + one-time URL). Writes (dangerous / "
        "approval-required): place a file into the guest (file.write) and run a "
        "program in the guest (program.run -- freeform in-guest execution via "
        "StartProgramInGuest, with optional exit-code polling). Guest OS "
        "credentials resolve from the target's Vault secret_ref (guest_username "
        "/ guest_password) and are NEVER a parameter. The right group for "
        "'what's running in this appliance?', 'read /etc/os-release from the "
        "guest', 'what does the guest think its network is?', 'drop this config "
        "file into the guest', or 'run Install-WindowsFeature inside the guest'. "
        "Requires VMware Tools in the guest."
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
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    # Optional per-op agent-facing usage instructions. Defaults to
    # ``None`` so the existing rows (whose ``description`` already carries
    # the guidance) are unchanged; the guest-ops family (#3100) sets it.
    llm_instructions: dict[str, Any] | None = None


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
            "POST:/vcenter/vm/{vm}/power start. On pre-9.0 vCenter (live "
            "about.version major < 9) the create instead rides vim "
            "Folder.CreateVM_Task through the governed vmomi substrate, "
            "task-polled, with NICs and nested_hv folded into the one "
            "ConfigSpec — the bare REST create is vendor-defective on "
            "8.0.x; resource_pool and datastore are required there. "
            "Partial-failure rollback: if any step after the create "
            "succeeds fails, the half-created VM is removed via "
            "DELETE:/vcenter/vm/{vm} so the caller knows the VM did not "
            "persist. Equivalent of 'govc vm.create' for operator-facing "
            "dispatch."
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
        op_id="vmware.composite.vm.import_from_library",
        handler=vm_import_from_library_composite,
        summary="Import an OVF/OVA content-library item to a new VM via a typed HttpNfcLease.",
        description=(
            "The durable, transfer-window-decoupled counterpart to "
            "vm.deploy_from_library (#3229). deploy_from_library's synchronous REST "
            "deploy holds one POST open for the whole server-side copy, so completion "
            "is bounded by the client read-timeout — a multi-GB installer OVA outran "
            "even the 3h mitigation ceiling live (#3176). This composite drives the "
            "transfer itself over a typed vim HttpNfcLease import: ServiceContent "
            "resolve → OvfManager.CreateImportSpec (validates the descriptor) → the "
            "governed ResourcePool.ImportVApp write → poll the lease to ready → stream "
            "each disk from the content library (client-side download-session) straight "
            "to the lease device URLs with an HttpNfcLeaseProgress heartbeat → "
            "HttpNfcLeaseComplete. Completion is bounded only by the transfer's own "
            "duration, not any single HTTP read, and under async governed dispatch "
            "(#3079) a dropped caller no longer aborts it. Version-agnostic (core "
            "vim25 OvfManager / HttpNfcLease; no 9.0-only fields), so it also covers "
            "the pre-9.0 VCF 5.x migration-source fleet (#3056). The library item is "
            "referenced by id (passthrough) or name (resolved via "
            "POST:/content/library/item?action=find, ambiguity-refused before any "
            "mutation); resource_pool and datastore are required. Any failure after "
            "the lease exists aborts it, so vCenter removes the half-created VM. Maps "
            "to the same deployed / deploy_failed / deploy_error envelope (#3071) as "
            "the deploy composite, plus a per-disk transfer manifest. Equivalent of a "
            "task-polled 'govc library.deploy' for large OVAs."
        ),
        parameter_schema=VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
        response_schema=VM_IMPORT_FROM_LIBRARY_RESPONSE_SCHEMA,
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
        op_id="vmware.composite.vm.destroy",
        handler=vm_destroy_composite,
        summary="Permanently destroy (delete) a powered-off VM and all its disks.",
        description=(
            "The FIRST governed destructive delete (#3198), decision "
            "docs/decisions/governed-delete-operations.md. Permanently "
            "destroys a VM and all of its virtual disks. Marked "
            "safety_level='destructive' — the hardest gate MEHO has: "
            "mandatory human approval always (no agent path, no standing "
            "grant, no self-approval even under break-glass), a mandatory "
            "preview-hash binding, and a mandatory blast-radius statement "
            "(object identity + enumerated disks/NICs/snapshots + "
            "irreversibility class) the four-eyes approver reads before "
            "deciding. Fail-closed on a running VM: a VM that is not "
            "POWERED_OFF is refused with status='not_powered_off' and never "
            "powered off implicitly. Dual arm (like vm.create): a resolvable "
            "pre-9.0 vCenter routes through the vim VirtualMachine.Destroy_Task "
            "(task-polled); 9.0+ (and unresolved) issues the synchronous REST "
            "DELETE:/vcenter/vm/{vm}. Equivalent of 'govc vm.destroy' for "
            "governed operator-facing dispatch."
        ),
        parameter_schema=VM_DESTROY_PARAMETER_SCHEMA,
        response_schema=VM_DESTROY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "destroy", "destructive"],
        safety_level="destructive",
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
        op_id="vmware.composite.vm.disk.attach",
        handler=vm_disk_attach_composite,
        summary="Attach an existing VMDK to a VM at an explicit SCSI controller/unit address.",
        description=(
            "Attaches an EXISTING VMDK to a VM at a caller-specified SCSI "
            "controller_key + unit_number — the shared-attach leg of a Windows "
            "Server Failover Cluster (WSFC) / SQL FCI or a multi-writer cluster "
            "(the second node opens the same disk the first node created, at "
            "the same address). Rides the #2893 mutating VI-JSON substrate: a "
            "RetrievePropertiesEx read locates the SCSI controller and confirms "
            "the unit is free, then a single ReconfigVM_Task carries a "
            "VirtualDeviceConfigSpec add with NO fileOperation (attach, not "
            "create). Validates vmdk_path ('[datastore] path.vmdk') and the "
            "unit (0-15, not the reserved 7) before any write; refuses a "
            "missing/non-SCSI controller or an occupied unit. Optional "
            "sharing='multi_writer' sets the backing's sharingMultiWriter (for "
            "application-managed clustering); WSFC instead uses a physical "
            "bus-sharing controller (vm.create scsi_bus_sharing), not "
            "multi-writer. The REST Disk.CreateSpec.backing can attach an "
            "existing VMDK but cannot set the sharing flag, so this path is "
            "vim-uniform (8.x + 9.x). Equivalent of 'govc device.scsi.add' + "
            "'govc vm.disk.attach'."
        ),
        parameter_schema=VM_DISK_ATTACH_PARAMETER_SCHEMA,
        response_schema=VM_DISK_ATTACH_RESPONSE_SCHEMA,
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
        op_id="vmware.composite.network.portgroup.create",
        handler=network_portgroup_create_composite,
        summary="Create a distributed portgroup on a DVS with a VLAN trunk (or access) spec.",
        description=(
            "Creates a distributed portgroup on an existing DVS -- the L2 "
            "substrate a nested-hypervisor lab attaches to. The pinned "
            "vcenter.yaml serves no portgroup-create REST path, so the create "
            "goes through vim DistributedVirtualSwitch.CreateDVPortgroup_Task "
            "with one DVPortgroupConfigSpec (name, binding type, optional "
            "numPorts) and a VMwareDVSPortSetting.vlan -- either a VLAN trunk "
            "(VmwareDistributedVirtualSwitchTrunkVlanSpec NumericRange[], e.g. "
            "0-4094 during bootstrap) or a single access VLAN "
            "(VmwareDistributedVirtualSwitchVlanIdSpec). The returned task is "
            "polled to a terminal state; the new portgroup's config is read "
            "back (name + vlan) for verification. vds is the switch moid (no "
            "portgroup/switch REST list exists to resolve a name); passing "
            "both trunk and access VLAN returns status='invalid_vlan_spec' "
            "before any write. Equivalent of 'govc dvs.portgroup.add'."
        ),
        parameter_schema=NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.security.set",
        handler=network_portgroup_security_set_composite,
        summary=(
            "Set a distributed portgroup's security policy "
            "(promiscuous / forged-transmits / MAC-changes)."
        ),
        description=(
            "Sets any of a distributed portgroup's security-policy triple -- "
            "allowPromiscuous / forgedTransmits / macChanges -- the knobs a "
            "nested-ESXi trunk portgroup needs at Accept. The security policy "
            "lives in DVPortgroupConfigSpec.defaultPortConfig.securityPolicy "
            "with no REST expression (govc exposes no flags for it), so the "
            "change goes through vim "
            "DistributedVirtualPortgroup.ReconfigureDVPortgroup_Task. The "
            "required configVersion and the current policy are read first (the "
            "before-state the four-eyes reviewer sees); only the booleans you "
            "supply are written; the applied policy is read back after. No "
            "boolean supplied returns status='no_change_requested' before any "
            "write. Governance-sensitive: promiscuous mode makes the portgroup "
            "see all traffic on its VLANs. Equivalent of PowerCLI "
            "'Set-VDSecurityPolicy'."
        ),
        parameter_schema=NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
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
    # ----------------------------------------------------------------
    # Host-domain write composites (#3182) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.host.datastore_mount_nfs",
        handler=datastore_mount_nfs_composite,
        summary="Mount an NFS export as a datastore on a host via CreateNasDatastore.",
        description=(
            "Mounts an NFS v3/v4.1 export as a datastore on one host via the "
            "synchronous vim HostDatastoreSystem.CreateNasDatastore — the pinned "
            "vcenter.yaml serves no host NAS-mount REST path, so vim is the sole "
            "governed path (#3182). Resolves the host by display name or moref "
            "(GET:/vcenter/host), then reads the host's "
            "HostSystem.configManager.datastoreSystem MoRef and mounts the method "
            "on it, building a HostNasVolumeSpec from nfs_server / remote_path / "
            "datastore_name / access_mode / nfs_type. The 200 body is the new "
            "Datastore MoRef directly (no task poll), so the composite returns "
            "status='mounted' with the datastore moid + a mount summary. A host "
            "that does not resolve uniquely refuses before any write "
            "(status='host_not_found' / 'ambiguous_host'). Nested-VCF hosts mount "
            "a base-layer NFS export as principal storage this way."
        ),
        parameter_schema=HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA,
        response_schema=HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "storage", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.disk_mark_flash",
        handler=disk_mark_flash_composite,
        summary="Mark host disks as flash (SSD) or non-flash (HDD) via MarkAs*_Task.",
        description=(
            "Marks one or more host disks as flash (SSD) or non-flash (HDD) via vim "
            "HostStorageSystem.MarkAsSsd_Task (mode='flash') / MarkAsNonSsd_Task "
            "(mode='non_flash') — the inverse is the same op keyed on the mode "
            "param, not a second op (#3182). Nested labs present virtual disks as "
            "HDD; vSAN-ready bring-up validation needs cache/capacity disks "
            "flash-marked. Resolves the host by display name or moref, reads its "
            "HostSystem.configManager.storageSystem MoRef, then per scsiDiskUuid "
            "issues the mark task through the governed vmomi seam and polls it to a "
            "terminal state. Set-shaped: one results row per disk (a per-disk fault "
            "/ timeout / transport error is captured, not aborted), aggregated into "
            "summary counts. Equivalent of 'govc host.storage.mark -ssd'."
        ),
        parameter_schema=HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA,
        response_schema=HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "storage", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.service_control",
        handler=service_control_composite,
        summary="Start/stop/restart a bounded host service + optionally set its policy.",
        description=(
            "Starts / stops / restarts a host service and optionally sets its "
            "startup policy via vim HostServiceSystem (synchronous StartService / "
            "StopService / RestartService + UpdateServicePolicy) — e.g. enabling "
            "host SSH (TSM-SSH) for a diagnostic / upgrade arc, then disabling it, "
            "leaving an audit row each time (#3182). Bounded to a curated "
            "server-side allowlist (TSM-SSH / TSM / ntpd / ptpd): an out-of-list "
            "service is refused with status='service_not_allowed' before any host "
            "resolution or write, never passed through (the no-arbitrary-exec "
            "posture). Resolves the host by display name or moref, reads its "
            "HostSystem.configManager.serviceSystem MoRef, applies the action "
            "through the governed vmomi seam, then (when policy is supplied) "
            "UpdateServicePolicy. Equivalent of 'govc host.service'."
        ),
        parameter_schema=HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
        response_schema=HOST_SERVICE_CONTROL_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "service", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
    ),
    # ----------------------------------------------------------------
    # Guest-operations channel (#3100) -- 4 safe reads + 1 dangerous write.
    # Guest OS credentials resolve from the target's secret_ref, never
    # from params.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.process.list",
        handler=guest_process_list_composite,
        summary="List processes running in a VM's guest OS via VMware Tools.",
        description=(
            "Lists the guest OS processes (name / pid / owner / cmdLine / "
            "startTime / exitCode) via the vim GuestProcessManager "
            "ListProcessesInGuest, authenticating with the guest credential "
            "resolved from the target's secret_ref. The exec-status shape: "
            "'what is running / did that service start / what exit code'. "
            "Governed replacement for 'govc guest.ps'. Read-only -- never "
            "mutates guest state. Requires VMware Tools in the guest."
        ),
        parameter_schema=GUEST_PROCESS_LIST_PARAMETER_SCHEMA,
        response_schema=GUEST_PROCESS_LIST_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "tools"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Inspect what is running inside a VM's guest OS -- verifying a "
                "service started, checking a process's exit code, triaging a "
                "first-boot appliance. Not for host/VM-level process state (that "
                "is the ESXi/vCenter surface), only in-guest processes."
            ),
            "preconditions": (
                "VMware Tools running in the guest; the target's Vault secret "
                "carries guest_username / guest_password. No credential goes in "
                "params."
            ),
            "result_shape": (
                "{vm, process_manager_moid, processes[], count, "
                "max_processes_applied}; the process list is JSONFlux-wrapped "
                "into a result handle when large -- drill in with result_query."
            ),
        },
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.env.read",
        handler=guest_env_read_composite,
        summary="Read environment variables from a VM's guest OS via VMware Tools.",
        description=(
            "Reads the guest user's environment variables (as NAME=value "
            "strings) via the vim GuestProcessManager "
            "ReadEnvironmentVariableInGuest, authenticating with the guest "
            "credential from the target's secret_ref. Omit 'names' for the whole "
            "environment, or pass specific names. Read-only. Requires VMware "
            "Tools in the guest."
        ),
        parameter_schema=GUEST_ENV_READ_PARAMETER_SCHEMA,
        response_schema=GUEST_ENV_READ_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "tools"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Read the guest OS environment of a running VM -- confirming a "
                "PATH, a proxy variable, or a first-boot configuration value the "
                "guest exports."
            ),
            "preconditions": (
                "VMware Tools running; guest credential in the target's "
                "secret_ref (guest_username / guest_password). Never in params."
            ),
            "result_shape": (
                "{vm, process_manager_moid, variables[], count}; variables is "
                "JSONFlux-wrapped when large."
            ),
        },
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.net.show",
        handler=guest_net_show_composite,
        summary="Show Tools-reported guest network state (no guest credentials).",
        description=(
            "Reads the VM's Tools-reported guest network state -- per-NIC IPs / "
            "MAC / connected (guest.net, GuestNicInfo) and routes / DNS / "
            "gateways (guest.ipStack, GuestStackInfo) -- via RetrievePropertiesEx. "
            "This is reported state on the VM object, so it needs NO in-guest "
            "authentication and runs nothing inside the guest: the 'read ip "
            "addr / routes' diagnosis without a guest login. Read-only."
        ),
        parameter_schema=GUEST_NET_SHOW_PARAMETER_SCHEMA,
        response_schema=GUEST_NET_SHOW_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "networking"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "See what network configuration a guest actually has (addresses, "
                "routes, DNS, gateways) as VMware Tools reports it -- e.g. "
                "diagnosing why an appliance is unreachable. The only guest read "
                "that needs no guest credentials."
            ),
            "preconditions": "VMware Tools running in the guest (reports the state).",
            "result_shape": "{vm, nics[], ip_stacks[]}.",
        },
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.file.read",
        handler=guest_file_read_composite,
        summary="Initiate a guest file read; returns size + attributes + transfer URL.",
        description=(
            "Initiates a guest file read via the vim GuestFileManager "
            "InitiateFileTransferFromGuest, authenticating with the guest "
            "credential from the target's secret_ref, and returns the "
            "FileTransferInformation (file size, POSIX/Windows attributes, and a "
            "one-time transfer URL) for guest_path. Inline byte retrieval is a "
            "deferred follow-up; this increment returns the transfer handle so "
            "existence + size + attributes are known without MEHO proxying the "
            "bytes. Read-only. Requires VMware Tools in the guest."
        ),
        parameter_schema=GUEST_FILE_READ_PARAMETER_SCHEMA,
        response_schema=GUEST_FILE_READ_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "file"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Confirm a file's existence / size / attributes in a guest OS, "
                "or obtain a one-time transfer URL for it. Inline content bytes "
                "are not returned in this increment."
            ),
            "preconditions": (
                "VMware Tools running; guest credential in the target's "
                "secret_ref. Guest user must be able to read guest_path."
            ),
            "result_shape": (
                "{vm, file_manager_moid, guest_path, url, size_bytes, "
                "attributes, content_fetch='deferred'}."
            ),
        },
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.file.write",
        handler=guest_file_write_composite,
        summary="Write a file into a VM's guest OS (dangerous / approval-required).",
        description=(
            "Writes UTF-8 content to guest_path inside a VM's guest OS via the "
            "vim GuestFileManager InitiateFileTransferToGuest (which mints a "
            "one-time PUT URL) followed by a direct PUT of the bytes -- the vim "
            "API's two-step design. Authenticates with the guest credential from "
            "the target's secret_ref (never in params). The single WRITE of the "
            "guest-ops channel: dangerous / approval-required, gated through the "
            "standard approvals plane -- a parked / denied approval mints no URL "
            "and PUTs no bytes. The park's proposed_effect echoes path + byte "
            "size + overwrite only, never the content. Requires VMware Tools."
        ),
        parameter_schema=GUEST_FILE_WRITE_PARAMETER_SCHEMA,
        response_schema=GUEST_FILE_WRITE_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "write", "guest", "vi-json", "file"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Place a config/repair file into a running guest OS (e.g. an "
                "MTU/network drop-in) without out-of-band scp. Approval-gated -- "
                "expect the call to park for a human decision unless a standing "
                "grant auto-executes it."
            ),
            "preconditions": (
                "VMware Tools running; guest credential in the target's "
                "secret_ref. Guest user must be able to write guest_path."
            ),
            "result_shape": (
                "On execute: {status='written', vm, file_manager_moid, "
                "guest_path, size_bytes, overwrite}. On gate: the approval "
                "OperationResult verbatim (awaiting_approval / denied)."
            ),
        },
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.program.run",
        handler=guest_program_run_composite,
        summary="Run a program in a VM's guest OS (dangerous / approval-required).",
        description=(
            "Runs a program inside a VM's guest OS via the vim "
            "GuestProcessManager StartProgramInGuest, authenticating with the "
            "guest credential from the target's secret_ref (never in params). "
            "The governed replacement for out-of-band 'govc guest.run': the "
            "freeform in-guest execution tier #3100 deliberately deferred, now "
            "lifted. StartProgramInGuest is fire-and-forget and returns only a "
            "PID (no output is captured); with wait=true the op polls "
            "ListProcessesInGuest until the process exits or timeout_seconds "
            "elapses, returning the exit code and start/end times. "
            "dangerous / approval-required, gated through the standard "
            "approvals plane -- a parked / denied approval starts no program. "
            "The 'arguments' string and 'env' values may carry secrets and are "
            "redacted from the approval preview, the result, and logs. Requires "
            "VMware Tools."
        ),
        parameter_schema=GUEST_PROGRAM_RUN_PARAMETER_SCHEMA,
        response_schema=GUEST_PROGRAM_RUN_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "write", "guest", "vi-json", "exec"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Execute a command inside a running guest OS (e.g. "
                "Install-WindowsFeature, an AD DS promotion, a systemctl "
                "invocation) without out-of-band govc guest.run. Approval-gated "
                "-- expect the call to park for a human decision unless a "
                "standing grant auto-executes it. Set wait=true to get the exit "
                "code; note StartProgramInGuest captures no stdout, so redirect "
                "to a file and read it back with guest.file.read if you need "
                "output. Setting 'env' REPLACES the guest environment rather "
                "than augmenting it."
            ),
            "preconditions": (
                "VMware Tools running; guest credential in the target's "
                "secret_ref. Guest user must be allowed to run the program."
            ),
            "result_shape": (
                "On execute: {status, vm, process_manager_moid, program_path, "
                "pid, exit_code, start_time, end_time, wait}. status is "
                "'started' (wait=false), 'exited' (exit code captured), "
                "'timeout' (still running at timeout), or 'exit_unknown' (exit "
                "info aged out / Tools restarted). On gate: the approval "
                "OperationResult verbatim (awaiting_approval / denied)."
            ),
        },
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

    Scope: 38 composites total -- 9 read (T5 / #508 + the 4 guest-ops
    reads / #3100) + 29 write (T6 / #509 + the destructive-tier
    ``vm.destroy`` / #3198,
    #509, single-VM ``vm.power`` / #2301, the mutating VI-JSON
    ``vm.disk.grow`` / #2893 + the WSFC/FCI shared-attach
    ``vm.disk.attach`` / #3256, the folder-template
    ``vm.clone_from_template`` / #2894, the vim cluster / inventory writes
    ``cluster.drs_rule.create`` + ``folder.create`` / #2895, the #2891
    hardware writes ``vm.resize`` / ``vm.nic.repoint`` /
    ``vm.device.cdrom``, the two GOSC composites
    ``guest.customization_spec.create`` / ``vm.customize`` / #2892, the
    OVF/OVA content-library deploy ``vm.deploy_from_library`` / #2909, the
    three host-domain writes ``host.datastore_mount_nfs`` /
    ``host.disk_mark_flash`` / ``host.service_control`` / #3182, the vim
    portgroup writes ``network.portgroup.create`` /
    ``network.portgroup.security.set`` / #3091, the content-library import
    ``vm.import_from_library`` / #3229, and the two guest-ops writes
    ``vm.guest.file.write`` / #3100 + ``vm.guest.program.run`` / #3255). (The
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
            llm_instructions=spec.llm_instructions,
            embedding_service=embedding_service,
        )
