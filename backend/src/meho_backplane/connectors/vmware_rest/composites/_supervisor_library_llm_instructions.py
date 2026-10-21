# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``llm_instructions`` for the Supervisor, namespace, content library and guest
customization multi-step operations of ``vmware-rest``.

One entry per op id, read by :mod:`._register`. Each entry has the keys the
operation contract check wants: ``when_to_call``, ``output_shape`` and
``next_step``, plus ``preconditions`` for every op that changes something.
``parameter_hints`` is optional. The op descriptions stay short; the longer
guidance lives here.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS"]

_P = "vmware.composite."

_LIBRARY_REF_HINTS: Final[dict[str, str]] = {
    "library_id": "Library id. If you give it, library_name is ignored.",
    "library_name": "Exact library name; it must match exactly one library.",
}

_LIBRARY_REF_PRECONDITION: Final = (
    "Give library_id or library_name. With neither, nothing happens (invalid_reference). "
    "A name must match exactly one library (else library_not_found or ambiguous_library)."
)

SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS: Final[dict[str, dict[str, Any]]] = {
    _P + "supervisor.enable": {
        "when_to_call": (
            "Call to turn on a vSphere Supervisor (the Kubernetes control plane of vSphere) "
            "on one cluster. Make the storage policy (storage_policy.create) and the "
            "Kubernetes release library (content_library.subscribed.create) first."
        ),
        "preconditions": (
            "The cluster has no Supervisor yet; otherwise vCenter refuses and the call "
            "fails. control_plane needs network and storage_policy, and workloads needs "
            "network.network_type and edge.provider; a missing one gives invalid_spec and "
            "an unknown network_type or provider gives unknown_network_provider, with no "
            "change. A second person must approve."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "control_plane.size": "TINY, SMALL, MEDIUM or LARGE; vCenter uses SMALL if left out.",
            "workloads.network.network_type": (
                "VSPHERE (vSphere networking), NSXT (NSX) or NSX_VPC (NSX VPC)."
            ),
            "workloads.edge.provider": (
                "VSPHERE_FOUNDATION (goes with VSPHERE), NSX, NSX_VPC, NSX_ADVANCED, or the "
                "deprecated HAPROXY."
            ),
        },
        "output_shape": (
            "{status: enabling|unknown_network_provider|invalid_spec, cluster, supervisor, "
            "network_type, edge_provider, guidance}. supervisor is the new Supervisor id, "
            "null on a refusal. A vCenter error (for example the cluster already has a "
            "Supervisor, or no licence) makes the call fail; it is not a status."
        ),
        "next_step": (
            "On enabling, call supervisor.status with the same cluster until ready is true "
            "(often 30 to 60 minutes), then namespace.create with the supervisor id. On a "
            "refusal, fix the field the guidance names and call again."
        ),
    },
    _P + "supervisor.disable": {
        "when_to_call": (
            "Call to remove the vSphere Supervisor from a cluster, for example to undo "
            "supervisor.enable or to enable it again with other settings."
        ),
        "preconditions": (
            "The cluster has a Supervisor. Its control plane VMs and worker nodes are "
            "removed, so the namespaces and Kubernetes workloads on it are lost. The "
            "cluster's networking and zone stay. A second person must approve."
        ),
        "parameter_hints": {"cluster": "Cluster moid, e.g. 'domain-c7'."},
        "output_shape": (
            "{status: disabling, cluster, guidance}. A vCenter error (for example the "
            "cluster has no Supervisor) makes the call fail; it is not a status."
        ),
        "next_step": (
            "Call supervisor.status with the cluster: config_status shows REMOVING while it "
            "runs. Once the Supervisor is gone, supervisor.status fails with a vCenter error; "
            "that means the removal is done."
        ),
    },
    _P + "supervisor.status": {
        "when_to_call": (
            "Call to see if the Supervisor of a cluster is ready, for example while "
            "supervisor.enable or supervisor.disable runs. Read-only."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, e.g. 'domain-c7'.",
            "messages_limit": "Most messages and conditions to return (default 25).",
        },
        "output_shape": (
            "{cluster, config_status: CONFIGURING|REMOVING|RUNNING|ERROR, kubernetes_status: "
            "READY|WARNING|ERROR, ready, messages: [{severity, details}], conditions: [{type, "
            "status, severity, reason, description}], message_count, condition_count}. ready "
            "is true only when config_status is RUNNING and kubernetes_status is READY. A "
            "cluster without a Supervisor makes the call fail with a vCenter error."
        ),
        "next_step": (
            "When ready is true, call namespace.create. While config_status is CONFIGURING, "
            "call again later. On ERROR, read messages and conditions; an operator must fix "
            "it in vCenter."
        ),
    },
    _P + "namespace.create": {
        "when_to_call": (
            "Call to make a vSphere Namespace on a ready Supervisor. A namespace is where a "
            "Kubernetes guest cluster (VKS) or VMs of the VM Service are created."
        ),
        "preconditions": (
            "The Supervisor is ready (supervisor.status gives ready true). namespace is a new "
            "DNS name: lower case letters, digits and '-', at most 63 characters. The storage "
            "policies, content libraries and VM classes you name must exist. A second person "
            "must approve."
        ),
        "parameter_hints": {
            "supervisor": "Supervisor id that supervisor.enable returned.",
            "namespace": "New namespace name, e.g. 'team-a'.",
            "storage_specs": "[{policy: <storage policy id>, limit: <MiB, optional>}].",
            "vm_service_spec": "{content_libraries: [<library id>], vm_classes: [<class name>]}.",
        },
        "output_shape": (
            "{status: created, namespace, supervisor, config_status, guidance}. config_status "
            "is null when vCenter does not show the namespace yet. A vCenter error (for "
            "example the name is taken) makes the call fail; it is not a status."
        ),
        "next_step": (
            "Call namespace.status until ready is true, then create the Kubernetes cluster "
            "in it. On config_status ERROR, read the messages of namespace.status."
        ),
    },
    _P + "namespace.delete": {
        "when_to_call": (
            "Call to delete a vSphere Namespace, for example to clean up after a test."
        ),
        "preconditions": (
            "Everything in the namespace is deleted with it: Kubernetes guest clusters, pods "
            "and volumes. Move or back up what you need first. A second person must always "
            "approve."
        ),
        "parameter_hints": {"namespace": "Exact namespace name."},
        "output_shape": (
            "{status: deleted|removing|still_present, namespace, config_status, guidance}. "
            "A vCenter error makes the call fail; it is not a status."
        ),
        "next_step": (
            "On deleted, nothing more to do. On removing, call namespace.status until exists "
            "is false. On still_present, read namespace.status and check vCenter before you "
            "retry."
        ),
    },
    _P + "namespace.status": {
        "when_to_call": (
            "Call to see if a vSphere Namespace exists and is ready, for example after "
            "namespace.create or namespace.delete. Read-only."
        ),
        "parameter_hints": {
            "namespace": "Exact namespace name.",
            "messages_limit": "Most messages to return (default 25).",
        },
        "output_shape": (
            "{namespace, exists, config_status: CONFIGURING|REMOVING|RUNNING|ERROR, ready, "
            "stats: {cpu_used, memory_used, storage_used}, description, messages: [{severity, "
            "details}], message_count}. A namespace that does not exist gives exists false, "
            "not an error."
        ),
        "next_step": (
            "When ready is true, create the Kubernetes cluster in the namespace. While "
            "config_status is CONFIGURING or REMOVING, call again later. On ERROR, read "
            "messages."
        ),
        "result_scalars": {
            "keys": [
                "namespace",
                "exists",
                "config_status",
                "ready",
                "description",
                "message_count",
            ]
        },
        "result_objects": {"objects": {"stats": ["cpu_used", "memory_used", "storage_used"]}},
    },
    _P + "content_library.subscribed.create": {
        "when_to_call": (
            "Call to make a content library that copies a published library, for example the "
            "VMware Kubernetes release library a Supervisor needs. For a library you fill "
            "yourself, use a local library."
        ),
        "preconditions": (
            "datastore must match exactly one datastore by name (else datastore_not_found or "
            "ambiguous_datastore, nothing is created). vCenter must reach subscription_url. "
            "Names need not be unique, so a second call makes a second library. For BASIC, "
            "give username and password. A second person must approve."
        ),
        "parameter_hints": {
            "datastore": "Datastore display name, not a moid.",
            "on_demand": "true (default) copies only the item list; files download on first use.",
            "password": "Secret. MEHO never shows it in a preview, a broadcast or an audit row.",
        },
        "output_shape": (
            "{status: created|datastore_not_found|ambiguous_datastore|create_error, "
            "library_id, name, subscription_url, datastore_id, on_demand, "
            "automatic_sync_enabled, candidates, issues: [{category, severity, message}]}. "
            "library_id is null unless status is created."
        ),
        "next_step": (
            "Call content_library.subscribed.status until last_sync_time is set, then "
            "content_library.subscribed.items.list. Pass library_id to namespace.create in "
            "vm_service_spec.content_libraries."
        ),
    },
    _P + "content_library.subscribed.sync": {
        "when_to_call": (
            "Call to make a subscribed library fetch the publisher's latest items now, for "
            "example when a new Kubernetes release is out."
        ),
        "preconditions": (
            _LIBRARY_REF_PRECONDITION + " The library must be a subscribed library. If a "
            "sync is already running, nothing new starts. A second person must approve."
        ),
        "parameter_hints": _LIBRARY_REF_HINTS,
        "output_shape": (
            "{status: sync_triggered|invalid_reference|library_not_found|ambiguous_library|"
            "sync_error, library_id, candidates, issues: [{category, severity, message}]}. "
            "sync_triggered means vCenter started the sync; it does not wait for it."
        ),
        "next_step": (
            "Call content_library.subscribed.status and compare last_sync_time, then "
            "content_library.subscribed.items.list. On ambiguous_library, call again with "
            "one of the candidates as library_id."
        ),
    },
    _P + "content_library.subscribed.status": {
        "when_to_call": (
            "Call to read the settings of a subscribed library and when it last synced, for "
            "example to see if the first sync after create is done. Read-only."
        ),
        "parameter_hints": _LIBRARY_REF_HINTS,
        "output_shape": (
            "{status: ok|invalid_reference|library_not_found|ambiguous_library|read_error, "
            "library_id, name, type, subscription_url, authentication_method, "
            "automatic_sync_enabled, on_demand, last_sync_time, description, "
            "storage_backings: [{type, datastore_id}], candidates, issues}. last_sync_time is "
            "null before the first sync."
        ),
        "next_step": (
            "When last_sync_time is set, call content_library.subscribed.items.list. If it "
            "stays null, call content_library.subscribed.sync. On ambiguous_library, call "
            "again with one of the candidates as library_id."
        ),
    },
    _P + "content_library.subscribed.items.list": {
        "when_to_call": (
            "Call to list the items of a subscribed library, for example the Kubernetes "
            "release versions a Supervisor can use. Read-only."
        ),
        "parameter_hints": _LIBRARY_REF_HINTS,
        "output_shape": (
            "{library_id, item_count, items: [{id, name, type, version, cached, size, "
            "last_sync_time}]} on success (no status field). On a lookup problem: {status: "
            "invalid_reference|library_not_found|ambiguous_library, library_id: null, "
            "candidates, issues}. A long items list comes back as a result handle; read it "
            "with result_query. At most 500 items are read."
        ),
        "next_step": (
            "cached false means the item's files are not downloaded yet; with on_demand they "
            "download on first use. If the list is empty, check "
            "content_library.subscribed.status and sync the library."
        ),
    },
    _P + "content_library.delete": {
        "when_to_call": (
            "Call to delete a content library (local or subscribed), for example one that a "
            "test environment created. To delete one item, use content_library.item.delete."
        ),
        "preconditions": (
            "Nothing changes (precondition_failed) while another library subscribes to it, "
            "while a VM has one of its items in a CD-ROM or floppy drive, or while it has "
            "items and delete_items is not true. Delete subscribed libraries before the "
            "library they subscribe to. A second person must always approve, after seeing "
            "the items that will be deleted."
        ),
        "parameter_hints": {
            "library_id": "Library id, a UUID (preferred).",
            "library_name": "Library name; a name that matches more than one is refused.",
            "delete_items": "true to delete a library that still has items.",
        },
        "output_shape": (
            "{status: deleted|unchanged|invalid_request|precondition_failed|still_present, "
            "object: {kind, id, name, type, published, datastores, item_count}, blockers: "
            "[{kind: subscribed_library, id, name, vcenter} or {kind: vm, moid, name, files}], "
            "task, task_state, guidance}. unchanged means the library did not exist."
        ),
        "next_step": (
            "On precondition_failed, deal with the blockers (delete the subscribing "
            "libraries, take the media out of the VMs) or set delete_items, then call again. "
            "On invalid_request, call again with library_id. On still_present, check vCenter "
            "before you retry."
        ),
    },
    _P + "content_library.item.delete": {
        "when_to_call": (
            "Call to delete one item (an OVF, ISO or template) from a local content library. "
            "To delete a whole library, use content_library.delete."
        ),
        "preconditions": (
            "The item must be in a local library on this vCenter; items of a subscribed "
            "library come from the publisher and are refused (precondition_failed). Nothing "
            "changes while a VM has the item in a CD-ROM or floppy drive. A second person "
            "must always approve, after seeing the item and its files."
        ),
        "parameter_hints": {
            "item_id": "Library item id, a UUID (preferred).",
            "item_name": "Item name; also give library_id or library_name.",
        },
        "output_shape": (
            "{status: deleted|unchanged|invalid_request|precondition_failed|still_present, "
            "object: {kind, id, name, type, size_bytes, library_id, library_name, "
            "library_type}, blockers: [{kind: vm, moid, name, files}], task, task_state, "
            "guidance}. unchanged means the item did not exist."
        ),
        "next_step": (
            "On precondition_failed, take the media out of the VMs in blockers, or delete the "
            "item in the publisher's library. On invalid_request, call again with item_id. On "
            "still_present, check vCenter before you retry."
        ),
    },
    _P + "guest.customization_spec.create": {
        "when_to_call": (
            "Call to save a named guest customization spec: host name, fixed IP or DHCP per "
            "NIC, and DNS, for a Linux or Windows guest. A clone or vm.customize applies it "
            "by name."
        ),
        "preconditions": (
            "spec_name must be new in vCenter; otherwise vCenter refuses and the call fails. "
            "os_type linux uses domain and time_zone; windows uses the windows_* fields. "
            "Fields of the other OS are ignored. The password and product key fields are "
            "secrets. A second person must approve."
        ),
        "parameter_hints": {
            "interfaces": (
                "One entry per NIC, in NIC order. {ip_address, prefix, gateways} for a fixed "
                "address; an entry without ip_address uses DHCP."
            ),
            "windows_time_zone": "Microsoft time zone index, e.g. 85 for GMT.",
        },
        "output_shape": (
            "{status: created, spec_name, os_type}. A vCenter error makes the call fail; it "
            "is not a status."
        ),
        "next_step": (
            "Apply it with vm.customize on a powered-off VM, or pass spec_name as "
            "customization_spec_name to vm.clone_from_template."
        ),
    },
    _P + "vm.customize": {
        "when_to_call": (
            "Call to apply a saved guest customization spec to an existing VM, so that the "
            "guest gets its host name and network settings on the next boot."
        ),
        "preconditions": (
            "The VM must not be powered on (else precondition_failed, nothing changes). name "
            "must match exactly one VM (else not_found or ambiguous). The spec must exist "
            "(guest.customization_spec.create). A second person must approve."
        ),
        "parameter_hints": {
            "name": "VM display name, not a moid.",
            "spec_name": "Name of a saved customization spec.",
            "power_on": "true to start the VM right after, so the customization runs now.",
        },
        "output_shape": (
            "{status: customization_set|powered_on|not_found|ambiguous|precondition_failed, "
            "vm, name, spec_name, power_state, applies_on, candidates: [{vm, name, "
            "power_state}], guidance}. A vCenter error (for example an unknown spec) makes "
            "the call fail; it is not a status."
        ),
        "next_step": (
            "On customization_set, power the VM on with vm.power; the customization runs on "
            "that boot. On precondition_failed, power the VM off first. On ambiguous, the "
            "names in candidates are not unique; rename one VM."
        ),
    },
}
