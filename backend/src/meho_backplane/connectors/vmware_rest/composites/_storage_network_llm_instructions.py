# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``llm_instructions`` for the storage and network multi-step operations of ``vmware-rest``.

One entry per op id, read by :mod:`._register`. Each entry has the keys the
operation contract check wants: ``when_to_call``, ``output_shape`` and
``next_step``, plus ``preconditions`` for every op that changes something.
``parameter_hints`` and ``notes`` are optional. The op descriptions stay
short; the longer guidance lives here.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["STORAGE_NETWORK_LLM_INSTRUCTIONS"]

_P = "vmware.composite."

STORAGE_NETWORK_LLM_INSTRUCTIONS: Final[dict[str, dict[str, Any]]] = {
    _P + "datastore.usage": {
        "when_to_call": (
            "Call to see the capacity, free space and VM count of the datastores, for "
            "example before you place a VM or to find a full datastore. It is also the "
            "place to find a datastore moid."
        ),
        "parameter_hints": {
            "filter_names": "Exact datastore names. Leave it out to list every datastore.",
        },
        "output_shape": (
            "{datastores: [{id, name, type, capacity, free_space, vm_count, vm_names, "
            "enrichment_note}]}. capacity and free_space are bytes. vm_names holds at most "
            "20 names; vm_count is the full number. vm_count and vm_names are null when "
            "the VM read was skipped (see enrichment_note)."
        ),
        "next_step": (
            "Use id (e.g. 'datastore-17') as the datastore of datastore.refresh, "
            "datastore.dir.create or datastore.file.delete. If the free space of an NFS "
            "datastore looks too small after its export grew, call datastore.refresh."
        ),
    },
    _P + "datastore.refresh": {
        "when_to_call": (
            "Call when the free space or capacity of a datastore is out of date, for "
            "example after an NFS export grew. ESXi keeps the old numbers until it reads "
            "the volume again; this op makes it read again. Read-only."
        ),
        "parameter_hints": {
            "datastore": (
                "On vCenter a moid such as 'datastore-17' (from datastore.usage); on a "
                "standalone ESXi host the NAS name '<server>:/<export>'."
            ),
            "storage_info": "true also refreshes the per-VM storage use (slower).",
        },
        "output_shape": (
            "{datastore, refreshed: true, storage_info_refreshed, summary: {name, "
            "capacity, free_space, accessible, type, url}}. capacity and free_space are "
            "bytes, read after the refresh. A vSphere error makes the call fail."
        ),
        "next_step": (
            "Check summary.free_space against what you need, then go on with the deploy. "
            "If summary.accessible is false, the host cannot reach the datastore: check "
            "the NFS server or the storage network first."
        ),
    },
    _P + "datastore.dir.create": {
        "when_to_call": (
            "Call to create a folder on a datastore, for example for ISO files or install "
            "media. It is safe to run twice: a folder that exists is left alone."
        ),
        "preconditions": (
            "datastore is a moid of a VMFS or NFS datastore on a vCenter target (vSAN and "
            "vVol are refused). path is relative to the datastore root and follows the "
            "path rules of the field. The parent folder must exist, unless "
            "create_parents=true. No file may have the same name."
        ),
        "parameter_hints": {
            "datastore": "Datastore moid, e.g. 'datastore-17'.",
            "path": "Relative path, e.g. 'iso' or 'stage/media'.",
            "create_parents": "true also creates missing parent folders.",
        },
        "output_shape": (
            "{status: created|unchanged|invalid_request|precondition_failed|not_verified, "
            "object: {kind, datastore, path, datastore_name, datastore_type, "
            "datastore_path}, blockers: [], task, task_state, guidance}. Every status "
            "except created changed nothing, or could not prove the change."
        ),
        "next_step": (
            "On created or unchanged, use object.datastore_path ('[<datastore>] <path>') "
            "where a datastore path is needed. On precondition_failed, read guidance: pass "
            "create_parents=true or choose another name. On not_verified, list the "
            "datastore before you retry."
        ),
    },
    _P + "datastore.file.delete": {
        "when_to_call": (
            "Call to delete a file, or a folder with all it holds, that a teardown left on "
            "a datastore: the folder of a deleted VM, an old ISO, an installer folder. To "
            "delete a VM, use vm.destroy; files of registered VMs are refused here."
        ),
        "preconditions": (
            "datastore is a moid of a VMFS or NFS datastore on a vCenter target. path is "
            "relative and follows the path rules of the field. No VM registered in this "
            "vCenter may use a file under path (disks, .vmx, snapshots, logs, a mounted "
            "ISO). A second person must always approve this delete, after seeing the size "
            "and the files."
        ),
        "parameter_hints": {
            "datastore": "Datastore moid, e.g. 'datastore-17'.",
            "path": "Relative path, e.g. 'old-appliance' or 'iso/stale.iso'.",
        },
        "output_shape": (
            "{status: deleted|unchanged|invalid_request|precondition_failed|"
            "still_present|timeout, object: {kind, datastore, path, datastore_name, "
            "datastore_type, datastore_path, resolved_path, file_type, size_bytes, "
            "modified}, blockers: [{kind: vm, moid, name, files}], task, task_state, "
            "guidance}. A vSphere error makes the call fail; it is not a status."
        ),
        "next_step": (
            "On deleted, nothing more to do. On unchanged, the path was already gone. On "
            "precondition_failed, the VMs in blockers use the files: delete or unregister "
            "them, or unmount the ISO, then call again. On still_present or timeout, call "
            "again to check."
        ),
    },
    _P + "storage_policy.list": {
        "when_to_call": (
            "Call to see the VM storage policies of vCenter, to find a policy id, or to "
            "check that storage_policy.create or storage_policy.delete worked. Read-only."
        ),
        "parameter_hints": {
            "policy_ids": "Policy ids to keep. Leave it out to list every policy.",
        },
        "output_shape": (
            "{policies: [{policy, name, description}]}. policy is the policy id. A long "
            "list comes back as a link to the stored result; read it with result_query."
        ),
        "next_step": (
            "Use policy as the policy_id of storage_policy.delete, or as the storage "
            "policy of a namespace or Supervisor."
        ),
    },
    _P + "storage_policy.create": {
        "when_to_call": (
            "Call to create a VM storage policy for NFS datastores, which have no default "
            "policy. It creates a tag category and a tag, puts the tag on the datastores, "
            "and creates a policy that asks for that tag. A Supervisor on NFS needs this "
            "first. Safe to run twice: objects with the same name are reused."
        ),
        "preconditions": (
            "Each name in datastore_names must match exactly one datastore (else "
            "datastore_not_found, no change). An existing tag category with this name "
            "must allow many tags per object and must allow datastores (else "
            "category_conflict). An existing policy with this name must have the same "
            "rule (else policy_conflict). A second person must approve."
        ),
        "parameter_hints": {
            "policy_name": "At most 80 characters.",
            "datastore_names": "Datastore display names, not moids.",
        },
        "output_shape": (
            "{status: created|adopted|datastore_not_found|category_conflict|"
            "policy_conflict|policy_create_failed, policy_id, policy_name, category_id, "
            "tag_id, tag_name, datastores: [{name, moid}], listed, adopted: {category, tag, "
            "policy}, issues, guidance}. adopted tells which parts already existed."
        ),
        "next_step": (
            "On created or adopted, use policy_id as the storage policy of a Supervisor or "
            "namespace. On policy_create_failed, call again: the tag parts are reused. On "
            "a *_conflict status, choose another name or fix the existing object."
        ),
    },
    _P + "storage_policy.delete": {
        "when_to_call": (
            "Call to delete one VM storage policy, for example the one storage_policy."
            "create made, when an environment is torn down. The tag and tag category stay."
        ),
        "preconditions": (
            "policy_id is the id storage_policy.list returns as policy. No VM, Supervisor "
            "or namespace may still use the policy (else delete_failed, no change; there "
            "is no force). A second person must always approve this delete."
        ),
        "parameter_hints": {
            "policy_id": "Policy id, from storage_policy.list.",
            "policy_name": "Only shown to the approver.",
        },
        "output_shape": (
            "{status: deleted|delete_failed|still_present, policy_id, fault, issues, "
            "guidance}. fault is the vSphere fault type on delete_failed."
        ),
        "next_step": (
            "On delete_failed, free the policy (move the VMs, Supervisor or namespace to "
            "another policy), then call again. On still_present, list the policies again "
            "before you retry."
        ),
    },
    _P + "network.portgroup.audit": {
        "when_to_call": (
            "Call to list the distributed portgroups with their switch, VLAN and VMs, for "
            "example to find a portgroup moid or to check what uses a portgroup. Read-only."
        ),
        "parameter_hints": {
            "filter_dvs": "Switch moid (e.g. 'dvs-31') or switch name.",
            "include_disconnected_vms": "true also counts VMs that are not powered on.",
        },
        "output_shape": (
            "{portgroups: [{id, name, dvs, dvs_name, type, vlan: {mode: access|trunk, "
            "vlan_id, ranges: [{start, end}]}, vm_count, vm_names}]}. vlan is null for a "
            "private VLAN. A long list comes back as a link to the stored result."
        ),
        "next_step": (
            "Use id (e.g. 'dvportgroup-42') as the portgroup of portgroup.vlan.set, "
            "portgroup.security.set or portgroup.delete, and dvs as the vds of "
            "portgroup.create."
        ),
    },
    _P + "network.portgroup.create": {
        "when_to_call": (
            "Call to create a distributed portgroup on a distributed switch, with a VLAN "
            "trunk (for example for nested ESXi hosts) or one access VLAN."
        ),
        "preconditions": (
            "vds is the moid of an existing distributed switch (from portgroup.audit); "
            "names are not accepted. Give vlan_trunk_ranges or vlan_id, not both; MEHO "
            "refuses both before the call. Give neither to keep the switch's default "
            "VLAN. The name must be new on the switch, or vCenter refuses it."
        ),
        "parameter_hints": {
            "vds": "Distributed switch moid, e.g. 'dvs-31'.",
            "vlan_trunk_ranges": "e.g. [{start: 0, end: 4094}] for all VLANs.",
            "vlan_id": "One access VLAN; 0 means no VLAN.",
        },
        "output_shape": (
            "{status: created|invalid_vlan_spec|timeout, portgroup, vds, name, task, "
            "observed: {name, vlan}, guidance, issues}. portgroup is the new moid. "
            "observed.vlan is the VLAN as the switch reports it. A vSphere error makes "
            "the call fail."
        ),
        "next_step": (
            "On created, check observed.vlan, then set the security policy with "
            "portgroup.security.set if nested ESXi hosts use it. On timeout, list the "
            "portgroups with portgroup.audit before you retry."
        ),
    },
    _P + "network.portgroup.security.set": {
        "when_to_call": (
            "Call to turn promiscuous mode, forged transmits or MAC address changes on or "
            "off for one distributed portgroup. Nested ESXi hosts need all three on."
        ),
        "preconditions": (
            "portgroup is a distributed portgroup moid. Give at least one of the three "
            "booleans; with none, nothing changes (no_change_requested). Only the "
            "booleans you give are written. Promiscuous mode lets every VM on the "
            "portgroup see all traffic on its VLANs, so turn it on only where needed."
        ),
        "parameter_hints": {"portgroup": "Distributed portgroup moid, e.g. 'dvportgroup-42'."},
        "output_shape": (
            "{status: updated|no_change_requested|timeout, portgroup, requested, previous, "
            "observed, task, guidance, issues}. requested, previous and observed are each "
            "{allow_promiscuous, forged_transmits, mac_changes}. A vSphere error makes "
            "the call fail."
        ),
        "next_step": (
            "On updated, compare observed with requested. On timeout, read the portgroup "
            "again before you retry."
        ),
    },
    _P + "network.portgroup.vlan.set": {
        "when_to_call": (
            "Call to change the VLAN of an existing distributed portgroup: a new trunk "
            "range list or one access VLAN. Safe to run twice: when the VLAN already "
            "matches, nothing is written."
        ),
        "preconditions": (
            "portgroup is a distributed portgroup moid. Give exactly one of "
            "vlan_trunk_ranges or vlan_id; MEHO refuses both or neither before the call. "
            "The new VLAN replaces the old one: to add a VLAN to a trunk, pass the full "
            "list you want. VMs on the portgroup lose traffic on VLANs you leave out."
        ),
        "parameter_hints": {
            "portgroup": "Distributed portgroup moid, e.g. 'dvportgroup-42'.",
            "vlan_trunk_ranges": "Full list, e.g. [{start: 0, end: 0}, {start: 100, end: 200}].",
            "replace": "Leave it out; only true is supported.",
        },
        "output_shape": (
            "{status: set|unchanged|invalid_vlan_spec|timeout, portgroup, requested, "
            "before, after, task, guidance, issues}. requested, before and after are each "
            "{mode: trunk, ranges: [{start, end}]} or {mode: access, vlan_id}; touching "
            "ranges are merged. A vSphere error makes the call fail."
        ),
        "next_step": (
            "On set, compare after with requested. On invalid_vlan_spec, read guidance "
            "(replace=false is not supported). On timeout, read the portgroup again "
            "before you retry."
        ),
    },
    _P + "network.portgroup.delete": {
        "when_to_call": (
            "Call to delete a distributed portgroup that an environment created. For a "
            "port group on a host's standard switch, use host.standard_portgroup.delete."
        ),
        "preconditions": (
            "portgroup is a distributed portgroup moid (from portgroup.audit). No VM NIC, "
            "VMkernel adapter or other port may use it, and it must not be the switch's "
            "uplink portgroup or backed by an NSX segment (else precondition_failed, no "
            "change). A second person must always approve this delete."
        ),
        "parameter_hints": {"portgroup": "Distributed portgroup moid, e.g. 'dvportgroup-42'."},
        "output_shape": (
            "{status: deleted|unchanged|precondition_failed|still_present|timeout, "
            "object: {kind, moid, name, dvs, vlan, num_ports, port_binding, uplink, "
            "backing_type, port_check}, blockers: [{kind: vm, moid, name} or {kind: port, "
            "id, connectee_type, connected_entity, nic}], task, task_state, guidance}. "
            "A vSphere error makes the call fail; it is not a status."
        ),
        "next_step": (
            "On precondition_failed, move each VM NIC in blockers to another network "
            "(vm.nic.repoint) and move the VMkernel adapters, then call again. On "
            "still_present or timeout, call again to check."
        ),
    },
    _P + "host.standard_portgroup.delete": {
        "when_to_call": (
            "Call once per host to remove a port group from that host's standard switch, "
            "for example a trunk port group an environment created. For a distributed "
            "portgroup, use network.portgroup.delete."
        ),
        "preconditions": (
            "host is a host moid on a vCenter target. portgroup_name is the exact port "
            "group name. No VM on this host (also a powered-off one) and no VMkernel "
            "adapter may use it (else precondition_failed, no change). Other hosts are "
            "not touched. A second person must always approve this delete."
        ),
        "parameter_hints": {
            "host": "Host moid, e.g. 'host-21'.",
            "portgroup_name": "Exact port group name on that host's standard switch.",
        },
        "output_shape": (
            "{status: deleted|unchanged|invalid_request|precondition_failed|"
            "still_present, object: {kind, host, name, host_name, vswitch, vlan: {mode: "
            "untagged|access|trunk_all, vlan_id}, active_ports}, blockers: [{kind: vm, "
            "moid, name} or {kind: vmkernel_adapter, id, name}], task, task_state, "
            "guidance}. A vSphere error makes the call fail."
        ),
        "next_step": (
            "On precondition_failed, move the VMs and VMkernel adapters in blockers to "
            "another port group, then call again. Repeat for each host that has the port "
            "group. On invalid_request, the host moid does not exist."
        ),
    },
}
