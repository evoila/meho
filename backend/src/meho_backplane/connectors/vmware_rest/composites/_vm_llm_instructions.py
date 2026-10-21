# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``llm_instructions`` for the VM and folder multi-step operations of ``vmware-rest``.

One entry per op id, read by :mod:`._register`. Each entry has the keys the
operation contract check wants: ``when_to_call``, ``output_shape`` and
``next_step``, plus ``preconditions`` for every op that changes something.
``parameter_hints`` and ``notes`` are optional. The op descriptions stay
short; the longer guidance lives here.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["VM_LLM_INSTRUCTIONS"]

_P = "vmware.composite."

VM_LLM_INSTRUCTIONS: Final[dict[str, dict[str, Any]]] = {
    _P + "vm.create": {
        "when_to_call": (
            "Call to create a new, empty VM: CPU, memory, NICs, optional data disks, "
            "optional nested virtualization and optional power-on. To copy an existing "
            "VM or template, use vm.clone, vm.clone_from_template or "
            "vm.deploy_from_library instead."
        ),
        "preconditions": (
            "Give the folder as folder_name or as the folder moid (folder). On a "
            "multi-datacenter vCenter pass the folder moid or resource_pool / datastore "
            "pins, because each datacenter has a folder named 'vm'. On vCenter before "
            "9.0, resource_pool and datastore are required. guest_os is a vSphere "
            "guest id such as UBUNTU_64."
        ),
        "parameter_hints": {
            "folder": "VM folder moid, e.g. 'group-v42'.",
            "nics": "One entry per NIC: {network: <network moid>, backing_type}.",
            "disks": "Data disks in GB; the boot disk is not created here.",
        },
        "output_shape": (
            "{status: created|rolled_back, vm_id, steps_succeeded: [...], failed_step, "
            "rollback_reason, rollback: {vm_id, outcome: removed|awaiting_approval|"
            "not_removed}, nested_hv, candidate_folders, issues}. On rolled_back, "
            "failed_step and issues say why. rollback is present only when a VM was "
            "already created; its outcome says what happened to that VM: removed = "
            "the VM is gone; awaiting_approval = the delete waits for approval and "
            "the VM exists until then; not_removed = the delete failed or was not "
            "allowed and the VM still exists. Never report the VM as gone unless the "
            "outcome is removed."
        ),
        "next_step": (
            "On created, read the new VM with vmware.vm.info (vm_id). On rolled_back "
            "without a rollback field, or with outcome removed, nothing is left: fix "
            "the cause named by failed_step and call again. On awaiting_approval, "
            "wait for the delete to be approved before calling again; approving it "
            "only removes rollback.vm_id and creates nothing. On not_removed, delete "
            "rollback.vm_id with vm.destroy before calling again. If the folder name "
            "was ambiguous, call again with one of candidate_folders as folder."
        ),
    },
    _P + "vm.clone": {
        "when_to_call": (
            "Call to make a new VM from a VM template stored in a content library. "
            "For a template VM in a folder use vm.clone_from_template; for an OVF/OVA "
            "item use vm.deploy_from_library."
        ),
        "preconditions": (
            "library_item must be the id of a VM template item in a content library. "
            "source_vm must be a VM moid that exists."
        ),
        "output_shape": (
            "{status: completed, task_id: null, vm_id, guidance}. The deploy is "
            "synchronous, so vm_id is the new VM."
        ),
        "next_step": (
            "Read the new VM with vmware.vm.info. Use vm.resize or vm.nic.repoint to "
            "adjust it, and vm.power to start it."
        ),
    },
    _P + "vm.clone_from_template": {
        "when_to_call": (
            "Call to clone a template VM that lives in a VM folder (a VM marked as "
            "template), optionally applying a saved guest customization spec in the "
            "same step."
        ),
        "preconditions": (
            "source_template is the template's display name and must be a template, not "
            "a normal VM. folder, resource_pool and datastore are moids. A "
            "customization_spec_name must already exist (see "
            "guest.customization_spec.create)."
        ),
        "output_shape": (
            "{status: cloned|template_not_found|ambiguous_template|not_a_template|"
            "timeout, source_template, source_template_id, new_vm_name, new_vm_id, "
            "folder, task, customization_spec_name, candidates, guidance}."
        ),
        "next_step": (
            "On cloned, read new_vm_id with vmware.vm.info. On ambiguous_template, "
            "rename the duplicate templates so the name is unique, then call again. On "
            "timeout, check the task with vmware.tasks.recent."
        ),
    },
    _P + "vm.deploy_from_library": {
        "when_to_call": (
            "Call to deploy an OVF/OVA item from a content library as a new VM or vApp. "
            "For a very large OVA whose copy takes hours, use vm.import_from_library."
        ),
        "preconditions": (
            "Give exactly one of library_item (id) or library_item_name; MEHO refuses "
            "both or neither before the call. resource_pool is required. Map each OVF "
            "network to a network moid in network_mappings. A service principal "
            "running this alone also needs a grant for the rename step, or the VM keeps "
            "its temporary name until someone approves the rename."
        ),
        "parameter_hints": {
            "library_name": "Only narrows a library_item_name lookup to one library.",
            "network_mappings": "{<OVF network name>: <network moid>}.",
        },
        "output_shape": (
            "{status: deployed|deploy_failed|deploy_error|invalid_reference|"
            "library_not_found|ambiguous_library|item_not_found|ambiguous_item|"
            "resolve_error, vm_id, resource_type, library_item_id, powered_on, "
            "issues: [{category, severity, message}], candidates}."
        ),
        "next_step": (
            "On deployed, read vm_id with vmware.vm.info. On deploy_failed, read "
            "issues (often a network mapping or placement problem) and fix the "
            "params; a cleanup warning in issues means a partial VM or vApp is still "
            "there. On ambiguous_item, call again with one of candidates as "
            "library_item."
        ),
        "notes": (
            "A failed deploy tries to remove what it left and adds one cleanup issue "
            "per object. Severity info means the object was removed. Severity warning "
            "means it is still there: its delete failed, waits for approval (the "
            "message names the request) or could not be checked. On a warning, approve "
            "the delete or remove the object by hand; do not report the cleanup as "
            "done. Approving a waiting rename, power-on or cleanup step runs only that "
            "step; it never deploys again."
        ),
    },
    _P + "vm.import_from_library": {
        "when_to_call": (
            "Call to import a large OVF/OVA item from a content library when "
            "vm.deploy_from_library would run too long. MEHO copies the disks itself, "
            "so the time is limited only by the copy. Works on vCenter 8 and 9."
        ),
        "preconditions": (
            "Give exactly one of library_item (id) or library_item_name; MEHO refuses "
            "both or neither before the call. resource_pool and datastore are required."
        ),
        "parameter_hints": {
            "network_mappings": "{<OVF network name>: <network moid>}.",
        },
        "output_shape": (
            "{status: same values as vm.deploy_from_library, vm_id, resource_type, "
            "library_item_id, powered_on, issues: [{category, severity, message}], "
            "transfer: [{path, device_id, size_bytes}], candidates}."
        ),
        "next_step": (
            "On deployed, read vm_id with vmware.vm.info. On deploy_failed or "
            "deploy_error, read issues; a failed import is cancelled, so vCenter "
            "removes the half-made VM."
        ),
    },
    _P + "vm.snapshot.revert": {
        "when_to_call": (
            "Call to put a VM back to a named snapshot, for example after a failed change."
        ),
        "preconditions": (
            "Everything the VM did after the snapshot is lost. The snapshot name must "
            "be unique on the VM: this op takes no snapshot id, so if two snapshots "
            "share the name it refuses with status ambiguous."
        ),
        "output_shape": (
            "{status: reverted|ambiguous|not_found|timeout, snapshot_id, "
            "candidates: [{name, snapshot}], guidance, issues}."
        ),
        "next_step": (
            "On ambiguous, rename one of the snapshots (outside MEHO) and call again. "
            "On timeout, check the task with vmware.tasks.recent. Then check the VM "
            "with vmware.vm.info."
        ),
    },
    _P + "vm.destroy": {
        "when_to_call": (
            "Call only to delete a VM and all its disks for good, for example to "
            "remove a test VM. It cannot be undone."
        ),
        "preconditions": (
            "The VM must be POWERED_OFF; MEHO never powers it off for you (use "
            "vm.power verb 'off' first). vm is the moid, not the name. A second person "
            "must approve every call, after seeing the VM, its disks, NICs and "
            "snapshots; an AI agent cannot run it alone."
        ),
        "output_shape": (
            "{status: destroyed|not_powered_off|not_found|timeout, vm_id, power_state, "
            "arm, task_id, guidance, issues}."
        ),
        "next_step": (
            "On not_powered_off, power the VM off and call again. On timeout, check "
            "the task with vmware.tasks.recent."
        ),
    },
    _P + "vm.migrate": {
        "when_to_call": (
            "Call to move a VM to another host in its cluster, either where DRS "
            "recommends or to a host you name."
        ),
        "preconditions": (
            "cluster is the moid of the cluster the VM is in. Without target_host, DRS "
            "must have a recommendation for this VM; MEHO does not pick a host for you."
        ),
        "output_shape": "{status: migrated|no_recommendation, target_host, source, guidance}.",
        "next_step": (
            "On no_recommendation, pick a host and call again with target_host. Read "
            "cluster.drs_recommendations to see what DRS suggests."
        ),
    },
    _P + "vm.power.bulk": {
        "when_to_call": (
            "Call to start, stop, suspend or reset many VMs at once, chosen by a "
            "filter. For one VM, or for a clean guest shutdown, use vm.power."
        ),
        "preconditions": (
            "Narrow the filter: an empty filter matches every VM on the vCenter. stop "
            "and reset are hard power actions and can lose unsaved guest data."
        ),
        "parameter_hints": {
            "filter": (
                "e.g. {names: [...]} or {folders: ['group-v42'], power_states: ['POWERED_OFF']}."
            ),
        },
        "output_shape": (
            "{results: [{vm, status: ok|error, error}], summary: {ok, error}, "
            "aborted_on_failure, issues}."
        ),
        "next_step": "Check the VMs that report error and retry them one by one with vm.power.",
    },
    _P + "vm.power": {
        "when_to_call": (
            "Call to change the power state of one VM: on, off, reset, or a clean "
            "guest_shutdown / guest_reboot through VMware Tools."
        ),
        "preconditions": (
            "vm is the moid, not the name. off and reset are hard and can lose unsaved "
            "guest data; prefer guest_shutdown. The guest verbs need VMware Tools "
            "running."
        ),
        "output_shape": (
            "{vm, verb, status: ok|error|tools_unavailable, error, error_type, "
            "guest_tools, issues}."
        ),
        "next_step": (
            "On tools_unavailable, use verb 'off' if a hard stop is acceptable. Check "
            "the new power state with vmware.vm.info."
        ),
    },
    _P + "vm.disk.grow": {
        "when_to_call": "Call to make one virtual disk of a VM larger.",
        "preconditions": (
            "Grow only: a size equal to or smaller than now is refused. The guest OS "
            "must still extend its partition and file system afterwards."
        ),
        "parameter_hints": {
            "disk": "Disk device id, e.g. '2000' (from the VM's hardware).",
            "capacity_bytes": "New total size in bytes, e.g. 107374182400 for 100 GiB.",
        },
        "output_shape": (
            "{status: grown|invalid_shrink|disk_not_found|timeout, vm, disk, task, "
            "from_capacity_bytes, to_capacity_bytes, delta_bytes, guidance, issues}."
        ),
        "next_step": (
            "Extend the partition inside the guest. On timeout, check vmware.tasks.recent."
        ),
    },
    _P + "vm.disk.attach": {
        "when_to_call": (
            "Call to attach a VMDK file that already exists to a VM, for example the "
            "shared disk of a cluster where the second node opens the disk the first "
            "node created. It does not create a disk."
        ),
        "preconditions": (
            "The SCSI controller (controller_key) must already exist on the VM and the "
            "unit must be free. Unit 7 is reserved. Use sharing 'multi_writer' only for "
            "software that manages shared writes itself; Windows failover clusters use "
            "a bus-sharing controller instead (vm.create scsi_bus_sharing)."
        ),
        "parameter_hints": {
            "vmdk_path": "'[<datastore name>] <folder>/<disk>.vmdk'.",
            "controller_key": "Device key of the SCSI controller, e.g. 1000.",
        },
        "output_shape": (
            "{status: attached|invalid_vmdk_path|invalid_unit|controller_not_found|"
            "unit_in_use|timeout, vm, vmdk_path, controller_key, unit_number, sharing, "
            "task, guidance}."
        ),
        "next_step": "On unit_in_use, pick another unit_number. Check the VM with vmware.vm.info.",
    },
    _P + "vm.resize": {
        "when_to_call": (
            "Call to change a VM's vCPU count, cores per socket or memory, for example "
            "after a clone kept the template's size."
        ),
        "preconditions": (
            "Pass at least one of cpu_count, cores_per_socket, memory_mib. On a "
            "powered-on VM an increase needs hot-add; a decrease or a cores_per_socket "
            "change needs the VM powered off."
        ),
        "output_shape": (
            "{status: resized|requires_power_off|no_change|partial, vm, name, "
            "power_state, applied: {cpu, memory}, from: {cpu_count, cores_per_socket, "
            "memory_MiB}, to: {...}, guidance, issues}."
        ),
        "next_step": (
            "On requires_power_off, power the VM off with vm.power, call again, then power it on."
        ),
    },
    _P + "vm.nic.repoint": {
        "when_to_call": (
            "Call to move one VM network adapter to another distributed or standard port group."
        ),
        "preconditions": (
            "The VM loses network on the old port group. A standard port group exists "
            "once per host, so its name can match several networks; then pass the "
            "exact network moid as network. backing_type must match the network kind."
        ),
        "parameter_hints": {
            "nic": "Adapter device id, e.g. '4000'.",
            "network": "Network moid; skips the name lookup.",
        },
        "output_shape": (
            "{status: repointed|not_found|ambiguous|invalid_request, vm, nic, "
            "mac_address, current_backing: {type, network, network_name, ...}, "
            "requested_backing: {portgroup_id, portgroup_name, backing_type}, "
            "candidates: [{network, name, type}], guidance}."
        ),
        "next_step": (
            "On ambiguous, call again with one of candidates[].network as network. "
            "Nothing changes on any status other than repointed."
        ),
    },
    _P + "vm.resource_allocation.show": {
        "when_to_call": (
            "Call to see whether a VM has a CPU or memory limit or reservation (for "
            "example when it is slow because it is capped), and to check the values "
            "before and after vm.resource_allocation.set. vmware.vm.info does not show "
            "these fields."
        ),
        "parameter_hints": {"vm": "VM moid, e.g. 'vm-42'."},
        "output_shape": (
            "{status: ok|vm_not_found, vm, name, cpu_allocation: {limit, reservation, "
            "shares: {level, shares}}, memory_allocation: {...}, guidance}. CPU is MHz, "
            "memory MB; limit -1 means unlimited."
        ),
        "next_step": "To change a value, call vm.resource_allocation.set.",
    },
    _P + "vm.resource_allocation.set": {
        "when_to_call": (
            "Call to cap a VM's CPU or memory (limit), guarantee it capacity "
            "(reservation), or remove a cap (limit -1)."
        ),
        "preconditions": (
            "Read the current values first with vm.resource_allocation.show. Pass only "
            "the fields you want changed. A limit of 0 is refused, and a limit may not "
            "be below the reservation. It takes effect at once on a running VM; to "
            "undo, call again with the old values."
        ),
        "parameter_hints": {
            "cpu_limit_mhz": "CPU limit in MHz (>= 1); -1 = unlimited.",
            "memory_limit_mb": "Memory limit in MB (>= 1); -1 = unlimited.",
        },
        "output_shape": (
            "{status: set|unchanged|invalid_request|vm_not_found|partial|timeout, vm, "
            "name, requested, before: {cpu_allocation, memory_allocation}, after: "
            "{...}, task, task_state, guidance}. A task fault is an error, not a status."
        ),
        "next_step": (
            "Compare before and after. On partial, read the VM again with "
            "vm.resource_allocation.show."
        ),
    },
    _P + "vm.device.cdrom": {
        "when_to_call": (
            "Call to remove a VM's CD-ROM drive, change what it is backed by, or "
            "disconnect it. Typical use: a host-local ISO ties the VM to one host and "
            "blocks vMotion; switch it to CLIENT_DEVICE."
        ),
        "preconditions": (
            "action 'update' needs backing. 'remove' deletes the device; the guest loses the drive."
        ),
        "parameter_hints": {"cdrom": "CD-ROM device id, e.g. '16000'."},
        "output_shape": (
            "{status: removed|updated|disconnected|invalid_request, vm, cdrom, action, "
            "current_backing: {type, iso_file, ...}, state, requested_backing, guidance}."
        ),
        "next_step": "Check the VM with vmware.vm.info.",
    },
    _P + "folder.create": {
        "when_to_call": "Call to create a VM folder under a parent VM folder.",
        "preconditions": (
            "Give the parent as parent_folder (name) or parent_folder_id (moid). Each "
            "datacenter has a root VM folder named 'vm', so on a multi-datacenter "
            "vCenter pass parent_folder_id or datacenter."
        ),
        "output_shape": (
            "{status: created|parent_not_found|ambiguous_parent, parent_folder, "
            "parent_folder_id, datacenter, new_folder_name, folder, candidate_folders, "
            "issues: [{category, severity, message}], guidance}."
        ),
        "next_step": (
            "Use folder (the new moid) as the folder of vm.create or vm.clone_from_template. "
            "On ambiguous_parent, call again with one of candidate_folders as "
            "parent_folder_id."
        ),
    },
    _P + "folder.delete": {
        "when_to_call": (
            "Call to remove an inventory folder that is no longer needed, after its VMs "
            "were deleted or moved."
        ),
        "preconditions": (
            "The folder must be empty; a folder with content is refused and its "
            "content listed. Root and datacenter folders are refused. A second person "
            "must always approve it."
        ),
        "parameter_hints": {"folder": "Folder moid, e.g. 'group-v1234'."},
        "output_shape": (
            "{status: deleted|unchanged|precondition_failed|still_present|timeout, "
            "object: {kind, moid, name, parent, child_type, child_count}, blockers: "
            "[{kind, moid, name}], task, task_state, guidance}."
        ),
        "next_step": (
            "On precondition_failed, move or delete each object in blockers, then call again."
        ),
    },
}
