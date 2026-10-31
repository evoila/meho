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
            "before you retry. A VM-Host rule's groups stay; remove them with "
            "cluster.drs_group.delete when no rule uses them."
        ),
    },
    _P + "cluster.drs_group.members.set": {
        "when_to_call": (
            "Call to add VMs to, or remove VMs from, an existing DRS VM group, for example "
            "when a new VM must follow a VM-Host rule. It does the same for hosts in a host "
            "group. To make new groups with a rule, use cluster.drs_vm_host_rule.create."
        ),
        "preconditions": (
            "cluster is a cluster moid. group_name is the exact name of a VM group or host "
            "group on it (else group_not_found). add and remove are VM names for a VM group, "
            "host names for a host group. Every name in add must be in the cluster (else "
            "names_not_found, no change). Give at least one of add or remove. A person must "
            "approve the change."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "add": "Display names, not moids.",
            "remove": "Display names; a name not in the group is skipped.",
        },
        "output_shape": (
            "{status: updated|unchanged|invalid_request|group_not_found|names_not_found|"
            "mismatch|timeout, cluster, cluster_name, group_name, group_kind: vm|host, rules: "
            "[{kind, name, key, rule_type}], members_before: [{moid, name}], "
            "member_count_before, added: [{moid, name}], removed: [{moid, name}], "
            "already_members, not_members, not_found, member_count_after, members: [{moid, "
            "name}], task, task_state, guidance}. members is the group read back after the "
            "change. A vSphere error makes the call fail; it is not a status."
        ),
        "next_step": (
            "On updated, check members. On unchanged, the group already had these members. "
            "On names_not_found, fix the names in not_found and call again. On mismatch or "
            "timeout, call again with the same names to check the group."
        ),
    },
    _P + "cluster.drs_group.delete": {
        "when_to_call": (
            "Call to remove one DRS VM group or host group from a cluster, for example a "
            "group left behind after cluster.drs_rule.delete. The VMs and hosts stay."
        ),
        "preconditions": (
            "cluster is a cluster moid. group_name is the exact group name. No rule may use "
            "the group: if one does, nothing changes (precondition_failed) and blockers name "
            "the rules. A second person must always approve this delete."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "group_name": "Exact name of the VM group or host group.",
        },
        "output_shape": (
            "{status: deleted|unchanged|invalid_request|precondition_failed|still_present|"
            "timeout, object: {kind, cluster, cluster_name, name, group_kind, members: "
            "[{moid, name}], member_count}, blockers: [{kind: drs_rule, name, key, "
            "rule_type}], task, task_state, guidance}. A vSphere error makes the call fail; "
            "it is not a status."
        ),
        "next_step": (
            "On deleted, nothing more to do. On unchanged, the group was already gone. On "
            "precondition_failed, delete the rules in blockers with cluster.drs_rule.delete "
            "first, then call again. On still_present or timeout, read the cluster again "
            "before you retry."
        ),
    },
    _P + "cluster.drs_vm_host_rule.create": {
        "when_to_call": (
            "Call to make a set of VMs run on (or stay away from) a set of hosts in a "
            "cluster. It creates a VM group, a host group and the rule in one step. For a "
            "VM-to-VM rule, use cluster.drs_rule.create. With use_existing_groups=true it "
            "puts the rule on two groups that already exist and makes no group."
        ),
        "preconditions": (
            "rule_name, vm_group_name and host_group_name must all be new on the cluster "
            "(else rule_exists or group_exists, no change). vms and hosts are display "
            "names in this cluster; at least one of each must be found. With "
            "use_existing_groups=true, leave out vms and hosts; both groups must exist "
            "(else group_not_found, no change). With "
            "mandatory=true, DRS and HA never break the rule, which can block "
            "maintenance mode."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "affine": "true: VMs run on these hosts. false: VMs stay away from them.",
        },
        "output_shape": (
            "{status: created|rule_exists|group_exists|group_not_found|insufficient_vms|"
            "insufficient_hosts|timeout, cluster, rule_name, vm_group_name, "
            "host_group_name, affine, mandatory, enabled, task, resolved_vms: [{vm, "
            "name}], resolved_hosts: [{host, name}], guidance}."
        ),
        "next_step": (
            "On created, check resolved_vms and resolved_hosts: names that were not found "
            "are left out. On rule_exists or group_exists, pick new names, or use "
            "use_existing_groups=true to reuse the groups. On group_not_found, check the "
            "group names. To change a group later, use cluster.drs_group.members.set. On "
            "timeout, "
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
            "Call to move every running VM off one ESXi host and then put the host into "
            "maintenance mode, for example before hardware work."
        ),
        "preconditions": (
            "The host must be in a cluster (else not_in_cluster, no change). When DRS is on "
            "and fully automated, vCenter moves the running VMs itself as part of the "
            "maintenance-mode request. Otherwise MEHO moves each running VM where DRS "
            "recommends (vm.migrate); a VM without a DRS suggestion is no_recommendation and "
            "stops the call before maintenance mode, unless tolerate_partial_failure=true. "
            "Powered-off VMs are not moved and may stay on the host. The other hosts need "
            "room for the VMs."
        ),
        "parameter_hints": {
            "host": "Host moid, e.g. 'host-21'.",
            "vsan_mode": (
                "What vSAN does with the host's data: ensureObjectAccessibility (default, "
                "keep objects reachable), evacuateAllData (move all data off), noAction."
            ),
        },
        "output_shape": (
            "{status: evacuated|partial|aborted|timeout|not_in_cluster, host, migrated_vms: "
            "[...], failed_vms: [{vm, error}], remaining_vms: [{vm, name, power_state, "
            "reason}], maintenance_entered, maintenance_task, moved_by: drs|meho|none, "
            "guidance, issues}. migrated_vms are the VMs that left the host, moved by MEHO or "
            "by vCenter. remaining_vms are the VMs the host still lists after the task, with "
            "the reason (powered_off, no_recommendation, still_listed_after_move, not_moved, "
            "...). On timeout the maintenance-mode "
            "task still runs under maintenance_task; the call did not fail. A faulted "
            "maintenance-mode task makes the call fail."
        ),
        "next_step": (
            "On aborted or partial, look at failed_vms (no_recommendation means DRS found "
            "no place) and move those VMs with vm.migrate and a target_host, then call "
            "again. On timeout, check the task with vmware.tasks.recent before you work on "
            "the host. On not_in_cluster, move the VMs with vm.migrate and a target_host."
        ),
    },
    _P + "host.maintenance.exit": {
        "when_to_call": (
            "Call to end maintenance mode on one ESXi host, for example after hardware "
            "work or after host.evacuate, so the host can run VMs again. A second person "
            "must approve the call."
        ),
        "preconditions": (
            "The host must be connected to vCenter (else not_connected, no change). The "
            "VMs that must run on the host must be powered on elsewhere or stay off until "
            "the host is back. A host that is not in maintenance mode answers unchanged "
            "and sends nothing, so the call is safe to run twice."
        ),
        "parameter_hints": {
            "host": "Host moid, e.g. 'host-21'.",
        },
        "output_shape": (
            "{status: exited|unchanged|not_connected|timeout, host, name, "
            "connection_state, in_maintenance_mode, ha_state, maintenance_task, guidance, "
            "issues}. On exited the host left maintenance mode and in_maintenance_mode is "
            "false. On timeout the exit task still runs under maintenance_task; the call "
            "did not fail. A faulted exit task makes the call fail, with the fault text."
        ),
        "next_step": (
            "On exited, read the vSAN object health (vmware.host.vsan_health) until it is green "
            "before you move VMs onto the host; vSAN copies back the data that changed "
            "while the host was away. On timeout, check the task with vmware.tasks.recent "
            "and read the host again. On not_connected, reconnect the host first."
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
