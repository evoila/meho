# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``llm_instructions`` for the host and cluster multi-step operations of ``vmware-rest``.

One entry per op id, read by :mod:`._register`. Each entry has the keys the
operation contract check wants: ``when_to_call``, ``output_shape`` and
``next_step``, plus ``preconditions`` for every op that changes something.
``parameter_hints`` and ``notes`` are optional. The op descriptions stay
short; the longer guidance lives here.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["HOST_CLUSTER_LLM_INSTRUCTIONS"]

_P = "vmware.composite."

HOST_CLUSTER_LLM_INSTRUCTIONS: Final[dict[str, dict[str, Any]]] = {
    _P + "cluster.drs_recommendations": {
        "when_to_call": (
            "Call to see if DRS is on for a cluster and how automatic it is, and "
            "optionally the moves DRS recommends right now. Read it before you move VMs "
            "or put a host into maintenance mode."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "include_recommendations_history": "true also returns the pending DRS moves.",
        },
        "output_shape": (
            "{cluster: {name, resource_pool}, drs: {enabled, defaultVmBehavior: "
            "manual|partiallyAutomated|fullyAutomated, vmotionRate, "
            "enableVmBehaviorOverrides, option}, recommendations_history: [{key, rating, "
            "reason, reasonText, migrationList}]}. drs is {} when vCenter has no DRS "
            "config; recommendations_history is there only when asked for."
        ),
        "next_step": (
            "If drs.enabled is false or defaultVmBehavior is manual, host.evacuate and "
            "cluster.patch cannot rely on DRS to move VMs. To act on a recommended move, "
            "call vm.migrate for that VM."
        ),
    },
    _P + "cluster.drs_rule.create": {
        "when_to_call": (
            "Call to keep two or more VMs on the same host (affinity) or on different "
            "hosts (anti-affinity). To bind VMs to a group of hosts, use "
            "cluster.drs_vm_host_rule.create."
        ),
        "preconditions": (
            "The VMs must be in the cluster; vms are VM display names. At least two of "
            "them must be found, or nothing changes (insufficient_vms). The rule_name must "
            "be new on the cluster (else rule_exists, no change)."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "vms": "VM display names, not moids.",
        },
        "output_shape": (
            "{status: created|rule_exists|insufficient_vms|timeout, cluster, rule_name, "
            "rule_type, enabled, task, resolved_vms: [{vm, name}], guidance, issues}. A "
            "vSphere error makes the call fail; it is not a status."
        ),
        "next_step": (
            "On created, check resolved_vms: names that were not found are left out of "
            "the rule. On rule_exists, pick another name or delete the old rule with "
            "cluster.drs_rule.delete. On timeout, check the task with "
            "vmware.tasks.recent."
        ),
    },
    _P + "cluster.drs_rule.delete": {
        "when_to_call": (
            "Call to remove one DRS rule of any type from a cluster, for example a "
            "keep-apart rule for VMs that were deleted. The VM and host groups stay."
        ),
        "preconditions": (
            "cluster is a cluster moid. rule_name is the exact rule name and must be "
            "unique on the cluster; two rules with the same name are refused "
            "(precondition_failed). A second person must always approve this delete."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "rule_name": "Exact rule name, as given to cluster.drs_rule.create.",
        },
        "output_shape": (
            "{status: deleted|unchanged|invalid_request|precondition_failed|"
            "still_present|timeout, object: {kind, cluster, cluster_name, name, key, "
            "rule_type, enabled, mandatory, vms, vm_count, vm_group, "
            "affine_host_group, anti_affine_host_group}, blockers: "
            "[{kind: drs_rule, key, rule_type}], task, task_state, guidance}. "
            "A vSphere error makes the call fail; it is not a status."
        ),
        "next_step": (
            "On deleted, nothing more to do. On unchanged, the rule was already gone. On "
            "precondition_failed, rename the duplicate rules in vCenter so the name is "
            "unique, then call again. On still_present or timeout, read the rules again "
            "before you retry."
        ),
    },
    _P + "cluster.drs_vm_host_rule.create": {
        "when_to_call": (
            "Call to make a set of VMs run on (or stay away from) a set of hosts in a "
            "cluster. It creates a VM group, a host group and the rule in one step. For a "
            "VM-to-VM rule, use cluster.drs_rule.create."
        ),
        "preconditions": (
            "rule_name, vm_group_name and host_group_name must all be new on the cluster "
            "(else rule_exists or group_exists, no change). vms and hosts are display "
            "names in this cluster; at least one of each must be found. With "
            "mandatory=true, DRS and HA never break the rule, which can block "
            "maintenance mode."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "affine": "true: VMs run on these hosts. false: VMs stay away from them.",
        },
        "output_shape": (
            "{status: created|rule_exists|group_exists|insufficient_vms|"
            "insufficient_hosts|timeout, cluster, rule_name, vm_group_name, "
            "host_group_name, affine, mandatory, enabled, task, resolved_vms: [{vm, "
            "name}], resolved_hosts: [{host, name}], guidance}."
        ),
        "next_step": (
            "On created, check resolved_vms and resolved_hosts: names that were not found "
            "are left out. On rule_exists or group_exists, pick new names. On timeout, "
            "check the task with vmware.tasks.recent."
        ),
    },
    _P + "cluster.patch": {
        "when_to_call": (
            "Call to update the ESXi software of every host in a cluster, one host at a "
            "time: maintenance mode on, apply the cluster image, maintenance mode off."
        ),
        "preconditions": (
            "Each host must have a desired software image in vSphere Lifecycle Manager "
            "that already holds the target version (vSphere 8.0 Update 1 or later). DRS "
            "should be fully automated, so maintenance mode can move the VMs off each "
            "host, and the cluster needs room for one host to be out. This can run for "
            "hours."
        ),
        "output_shape": (
            "{status: completed|stopped, cluster, patched_hosts: [...], failed_host, "
            "remaining_hosts: [...], failure_reason}. On stopped, failed_host may still "
            "be in maintenance mode."
        ),
        "next_step": (
            "On stopped, read failure_reason and fix the cause on failed_host. A new call "
            "starts again at the first host, so every host goes through maintenance mode "
            "again."
        ),
    },
    _P + "host.datastore_mount_nfs": {
        "when_to_call": (
            "Call to mount an NFS v3 or v4.1 export as a datastore on one ESXi host. It "
            "is safe to run twice: an export that is already mounted is not mounted again."
        ),
        "preconditions": (
            "The NFS server must allow this host to mount the export. On a vCenter "
            "target, host is required (display name or moid); on a standalone ESXi "
            "target it is ignored. datastore_name must not be in use on the host for a "
            "different export (else name_conflict, no change)."
        ),
        "parameter_hints": {
            "host": "Host display name or moid, e.g. 'host-21'.",
            "nfs_type": "NFS is v3, NFS41 is v4.1.",
        },
        "output_shape": (
            "{status: mounted|already_mounted|name_conflict|host_not_found|"
            "ambiguous_host|config_manager_unreadable|unsupported_host_target|"
            "host_required, host, datastore, summary: {datastore, name, nfs_server, "
            "remote_path, access_mode, type}, candidate_hosts, guidance, issues}. "
            "Every status except mounted changed nothing."
        ),
        "next_step": (
            "On mounted or already_mounted, use datastore (the moid) with "
            "vmware.composite.datastore.usage. On ambiguous_host, call again with one of "
            "candidate_hosts. On name_conflict, choose another datastore_name."
        ),
    },
    _P + "host.detach_from_vds": {
        "when_to_call": (
            "Call to take an ESXi host out of a distributed switch. It first moves the "
            "NICs of the VMs on that host to a standard-switch network, then removes the "
            "host from the switch."
        ),
        "preconditions": (
            "fallback_network must be a standard portgroup that exists on the host. "
            "Every NIC of every VM on the host is moved to it, also NICs that are not on "
            "this switch. VMkernel adapters of the host are not moved: move them off the "
            "switch first, or vCenter refuses the removal."
        ),
        "parameter_hints": {
            "dvs": "Distributed switch moid, e.g. 'dvs-31'.",
            "fallback_network": "Standard portgroup network moid, e.g. 'network-13'.",
        },
        "output_shape": (
            "{status: detached|incomplete|timeout, host, vms_migrated: [...], "
            "vm_migration_failures: [{vm, error}], guidance, issues}. On incomplete the "
            "host stays on the switch. A vSphere error on the removal makes the call fail."
        ),
        "next_step": (
            "On incomplete, fix the VMs in vm_migration_failures and call again. On "
            "timeout, check the task with vmware.tasks.recent."
        ),
    },
    _P + "host.disk_mark_flash": {
        "when_to_call": (
            "Call to make ESXi treat disks as flash (SSD) or as normal disks (HDD), for "
            "example so vSAN can use the virtual disks of a nested host."
        ),
        "preconditions": (
            "disk_uuids are the SCSI disk UUIDs (ScsiLun.uuid) of the host's disks; "
            "vmware.host.storage_devices lists them. On a vCenter target, host is "
            "required."
        ),
        "parameter_hints": {
            "host": "Host display name or moid, e.g. 'host-21'.",
            "mode": "flash marks as SSD, non_flash marks as HDD.",
        },
        "output_shape": (
            "{status: marked|partial|host_not_found|ambiguous_host|"
            "config_manager_unreadable|unsupported_host_target|host_required, host, mode, "
            "results: [{disk_uuid, status: marked|faulted|timeout|error, task, error}], "
            "summary: {marked, failed}, candidate_hosts, guidance, issues}."
        ),
        "next_step": (
            "On partial, read the error of each failed row in results and retry only "
            "those disks. On ambiguous_host, call again with one of candidate_hosts."
        ),
    },
    _P + "host.evacuate": {
        "when_to_call": (
            "Call to move every VM off one ESXi host and then put the host into "
            "maintenance mode, for example before hardware work."
        ),
        "preconditions": (
            "The host must be in a cluster with DRS on: each VM moves where DRS "
            "recommends (vm.migrate). The other hosts need room for these VMs. With "
            "tolerate_partial_failure=true the host enters maintenance mode even when "
            "some VMs could not be moved."
        ),
        "parameter_hints": {"host": "Host moid, e.g. 'host-21'."},
        "output_shape": (
            "{status: evacuated|partial|aborted, host, migrated_vms: [...], failed_vms: "
            "[{vm, error}], maintenance_entered, issues}. On aborted the host is not in "
            "maintenance mode. A failed maintenance-mode step makes the call fail."
        ),
        "next_step": (
            "On aborted or partial, look at failed_vms (no_recommendation means DRS found "
            "no place) and move those VMs with vm.migrate and a target_host, then call "
            "again."
        ),
    },
    _P + "host.service_control": {
        "when_to_call": (
            "Call to start, stop or restart one ESXi host service, and optionally set "
            "when it starts. Only TSM-SSH (SSH), TSM (ESXi Shell), ntpd and ptpd are "
            "allowed."
        ),
        "preconditions": (
            "service must be one of TSM-SSH, TSM, ntpd or ptpd; any other is refused "
            "(service_not_allowed, no change). On a vCenter target, host is required. "
            "Turn SSH off again when the work is done."
        ),
        "parameter_hints": {
            "host": "Host display name or moid, e.g. 'host-21'.",
            "policy": "on: start at boot. automatic: start with its firewall port. off: never.",
        },
        "output_shape": (
            "{status: applied|service_not_allowed|host_not_found|ambiguous_host|"
            "config_manager_unreadable|unsupported_host_target|host_required, host, "
            "service, action, policy, policy_updated, allowed_services, candidate_hosts, "
            "guidance, issues}."
        ),
        "next_step": (
            "On applied, the service changed. On ambiguous_host, call again with one of "
            "candidate_hosts."
        ),
    },
    _P + "resource_pool.create": {
        "when_to_call": (
            "Call to create a resource pool, inside another pool (parent) or at the top "
            "of a cluster (cluster), optionally with CPU and memory reservations, limits "
            "and shares."
        ),
        "preconditions": (
            "Give exactly one of parent (a resource pool moid) or cluster (a cluster "
            "moid); MEHO refuses both or neither before the call. A reservation must fit "
            "into what the parent can still give, or vCenter refuses it."
        ),
        "parameter_hints": {
            "cpu_allocation": "Values in MHz, e.g. {reservation: 2000, limit: -1}.",
            "memory_allocation": "Values in MB, e.g. {reservation: 4096}.",
        },
        "output_shape": (
            "{status: created|cluster_root_pool_unresolved|no_parent, name, parent, "
            "parent_source: resource_pool|cluster, cluster, resource_pool, "
            "verified_under_parent, guidance}. resource_pool is the new pool moid."
        ),
        "next_step": (
            "Use resource_pool as the resource_pool of vm.create or vm.clone_from_template. "
            "On cluster_root_pool_unresolved, call again with parent."
        ),
    },
    _P + "resource_pool.delete": {
        "when_to_call": (
            "Call to delete a resource pool. Its child pools and VMs are not deleted: "
            "vCenter moves them up to the parent pool."
        ),
        "preconditions": (
            "resource_pool is the moid of a pool that is not a cluster's root pool. A "
            "pool with child pools or VMs is refused (not_empty, no change) unless "
            "force=true."
        ),
        "parameter_hints": {"resource_pool": "Resource pool moid, e.g. 'resgroup-42'."},
        "output_shape": (
            "{status: deleted|not_empty|delete_unverified, resource_pool, forced, "
            "child_pool_count, child_vm_count, verified_absent, guidance}."
        ),
        "next_step": (
            "On not_empty, tell the user how many pools and VMs will move up, and call "
            "again with force=true only if they agree. On delete_unverified, read the "
            "pool again before you retry."
        ),
    },
}
