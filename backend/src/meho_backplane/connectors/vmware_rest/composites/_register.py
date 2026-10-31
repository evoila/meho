# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — per-composite registration-metadata table
# (one _CompositeSpec row per vmware-rest composite, field-table form); the
# sibling schemas.py declaration module carries the same marker. Predates
# this change (~1490 lines); splitting the metadata table is out of scope for
# the #3349 governed-subop wiring.

"""``register_vmware_composite_operations`` -- registrar for the 71 composites.

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

The 20 read composites (the five T5 / #508 reads + the datastore
cache-refresh read ``datastore.refresh`` / #3789 + the datastore file list
``datastore.file.list`` / #3432 + the vSAN folder list
``datastore.vsan.folder.list`` / #4022 + the orphan folder check
``datastore.orphan_dirs`` / #3814 + the VM allocation read
``vm.resource_allocation.show`` / #3880 + the 4 guest-ops reads
``vm.guest.process.list`` / ``vm.guest.env.read`` / ``vm.guest.net.show``
/ ``vm.guest.file.read`` / #3100 + the Supervisor / namespace /
storage-policy / content-library ``status`` + ``list`` reads) are
read-only. 19 of them pass ``safety_level="safe"`` +
``requires_approval=False`` -- overrides of T4's ``dangerous`` / ``True``
defaults. The exception is ``vm.guest.file.read``, promoted to
``caution`` in #3720 (it can fetch arbitrary guest bytes as the in-guest
login), so it auto-parks for agent / service principals while a human
seat still executes it immediately; see its caution row below. (The
former ``host.network_uplinks`` and ``host.vsan_health`` reads were
re-shipped as typed ops in #2258; see
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops`.) The 55 write
composites (T6 / #509, single-VM ``vm.power`` / #2301, the guest-ops
write ``vm.guest.file.write`` / #3100, the mutating
VI-JSON ``vm.disk.grow`` / #2893, the folder-template
``vm.clone_from_template`` / #2894, the vim cluster / inventory writes
``cluster.drs_rule.create`` + ``folder.create`` / #2895, the #2891
hardware writes -- ``vm.resize`` / ``vm.nic.repoint`` /
``vm.device.cdrom``, the two GOSC composites
``guest.customization_spec.create`` / ``vm.customize`` / #2892, the
OVF/OVA content-library deploy ``vm.deploy_from_library`` / #2909 +
``vm.import_from_library`` / #3229, the
three host-domain writes ``host.datastore_mount_nfs`` /
``host.disk_mark_flash`` / ``host.service_control`` / #3182, and the
later Supervisor / #3281, storage-policy, content-library / #3495 and
resource-pool / #3505 write families, the #3339 teardown deletes, the
DRS rule delete ``cluster.drs_rule.delete``, the VM snapshot create +
delete ``vm.snapshot.create`` / ``vm.snapshot.delete``, the licence assign
``license.assign``, and the two alarm changes ``alarm.acknowledge`` /
``alarm.reset`` / #4383) are all ``requires_approval=True``.
33 pin ``safety_level="dangerous"`` (T4's default, passed explicitly for
clarity at the call site; the helper would default to it anyway). 11 pin
``safety_level="caution"`` -- the VM snapshot create ``vm.snapshot.create``,
the two alarm changes ``alarm.acknowledge`` / ``alarm.reset`` / #4383 (they
change only the state of alarms), the datastore directory create
``datastore.dir.create`` / #3339, the reversible VM allocation write
``vm.resource_allocation.set`` / #3880, the #3505 governed-allocation writes
``resource_pool.create`` + the VM-Host affinity
``cluster.drs_vm_host_rule.create``, plus ``namespace.create`` /
``storage_policy.create`` / ``content_library.subscribed.create`` /
``content_library.subscribed.sync`` (``resource_pool.delete`` is a
``dangerous`` reparent, not a destroy). 11 pin
``safety_level="destructive"`` -- ``vm.destroy`` / #3198 (the first
destructive op -- the governed-delete tier, decision
``docs/decisions/governed-delete-operations.md``), ``namespace.delete``,
``storage_policy.delete``, the six #3339 teardown deletes
(``network.portgroup.delete``, ``host.standard_portgroup.delete``,
``folder.delete``, ``datastore.file.delete``, ``content_library.delete``,
``content_library.item.delete``), ``cluster.drs_rule.delete``, the DRS group
delete ``cluster.drs_group.delete`` and the VM snapshot delete
``vm.snapshot.delete``. The DRS group member change
``cluster.drs_group.members.set`` is ``caution``. (The
``caution`` tier is not writes-only:
``vm.guest.file.read`` above is a ``caution`` read / #3720.)
Each :class:`_CompositeSpec` row carries its own ``safety_level`` +
``requires_approval`` so the policy posture is implied by the row,
not by global state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NamedTuple

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest._content_library_schemas import (
    CONTENT_LIBRARY_GROUP_KEY,
    CONTENT_LIBRARY_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.alarm_state import (
    ALARMS_GROUP_KEY,
    ALARMS_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.composites._alarm import (
    alarm_acknowledge_composite,
    alarm_reset_composite,
)
from meho_backplane.connectors.vmware_rest.composites._alarm_schemas import (
    ALARM_ACKNOWLEDGE_LLM_INSTRUCTIONS,
    ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA,
    ALARM_ACKNOWLEDGE_RESPONSE_SCHEMA,
    ALARM_RESET_LLM_INSTRUCTIONS,
    ALARM_RESET_PARAMETER_SCHEMA,
    ALARM_RESET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_files import (
    datastore_dir_create_composite,
    datastore_file_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_list import (
    datastore_file_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_orphans import (
    datastore_orphan_dirs_composite,
)
from meho_backplane.connectors.vmware_rest.composites._datastore_vsan import (
    datastore_vsan_folder_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._drs_group import (
    cluster_drs_group_members_set_composite,
)
from meho_backplane.connectors.vmware_rest.composites._drs_group_delete import (
    cluster_drs_group_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._drs_rule_delete import (
    cluster_drs_rule_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    register_vmware_governed_subops,
)
from meho_backplane.connectors.vmware_rest.composites._guest import (
    guest_env_read_composite,
    guest_file_read_composite,
    guest_file_write_composite,
    guest_net_show_composite,
    guest_process_list_composite,
    guest_program_run_composite,
)
from meho_backplane.connectors.vmware_rest.composites._guest_event_perf_llm_instructions import (
    GUEST_EVENT_PERF_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._host import (
    datastore_mount_nfs_composite,
    disk_mark_flash_composite,
    service_control_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host_certificate_ops import (
    HOST_CERTIFICATE_COMPOSITES,
)
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._library import (
    content_library_subscribed_create_composite,
    content_library_subscribed_items_list_composite,
    content_library_subscribed_status_composite,
    content_library_subscribed_sync_composite,
)
from meho_backplane.connectors.vmware_rest.composites._library_delete import (
    content_library_delete_composite,
    content_library_item_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._library_local import (
    CONTENT_LIBRARY_CREATE_COMPOSITE,
)
from meho_backplane.connectors.vmware_rest.composites._library_upload import (
    CONTENT_LIBRARY_ITEM_UPLOAD_COMPOSITE,
)
from meho_backplane.connectors.vmware_rest.composites._license import (
    LICENSE_ASSIGN_LLM_INSTRUCTIONS,
    LICENSE_ASSIGN_PARAMETER_SCHEMA,
    LICENSE_ASSIGN_RESPONSE_SCHEMA,
    license_assign_composite,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory import (
    migration_inventory_composite,
    register_migration_inventory_dispatch_surface,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_schemas import (
    MIGRATION_GROUP_WHEN_TO_USE,
    MIGRATION_INVENTORY_DESCRIPTION,
    MIGRATION_INVENTORY_LLM_INSTRUCTIONS,
    MIGRATION_INVENTORY_OP_ID,
    MIGRATION_INVENTORY_PARAMETER_SCHEMA,
    MIGRATION_INVENTORY_RESPONSE_SCHEMA,
    MIGRATION_INVENTORY_SUMMARY,
)
from meho_backplane.connectors.vmware_rest.composites._namespace import (
    namespace_create_composite,
    namespace_delete_composite,
    namespace_status_composite,
)
from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_refresh_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)
from meho_backplane.connectors.vmware_rest.composites._snapshot import (
    vm_snapshot_create_composite,
    vm_snapshot_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._storage_policy import (
    storage_policy_create_composite,
    storage_policy_delete_composite,
    storage_policy_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor import (
    supervisor_disable_composite,
    supervisor_enable_composite,
    supervisor_status_composite,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import folder_delete_composite
from meho_backplane.connectors.vmware_rest.composites._teardown_network import (
    host_standard_portgroup_delete_composite,
    network_portgroup_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._vcenter_install_ops import (
    VCENTER_INSTALL_COMPOSITES,
)
from meho_backplane.connectors.vmware_rest.composites._vm_allocation import (
    vm_resource_allocation_set_composite,
    vm_resource_allocation_show_composite,
)
from meho_backplane.connectors.vmware_rest.composites._vm_cdrom_library_iso import (
    VM_CDROM_LIBRARY_ISO_COMPOSITES,
)
from meho_backplane.connectors.vmware_rest.composites._vm_cdrom_tools import (
    VM_CDROM_TOOLS_COMPOSITES,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_drs_rule_create_composite,
    cluster_drs_vm_host_rule_create_composite,
    cluster_patch_composite,
    folder_create_composite,
    guest_customization_spec_create_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    host_maintenance_exit_composite,
    network_portgroup_create_composite,
    network_portgroup_security_set_composite,
    network_portgroup_vlan_set_composite,
    resource_pool_create_composite,
    resource_pool_delete_composite,
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
    CLUSTER_DRS_GROUP_DELETE_PARAMETER_SCHEMA,
    CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA,
    CLUSTER_DRS_GROUP_MEMBERS_SET_PARAMETER_SCHEMA,
    CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA,
    CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
    CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA,
    CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
    CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA,
    CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA,
    CLUSTER_DRS_RULE_DELETE_RESPONSE_SCHEMA,
    CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA,
    CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA,
    CLUSTER_PATCH_PARAMETER_SCHEMA,
    CLUSTER_PATCH_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA,
    CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA,
    DATASTORE_DIR_CREATE_PARAMETER_SCHEMA,
    DATASTORE_DIR_CREATE_RESPONSE_SCHEMA,
    DATASTORE_FILE_DELETE_PARAMETER_SCHEMA,
    DATASTORE_FILE_DELETE_RESPONSE_SCHEMA,
    DATASTORE_FILE_LIST_PARAMETER_SCHEMA,
    DATASTORE_FILE_LIST_RESPONSE_SCHEMA,
    DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA,
    DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA,
    DATASTORE_REFRESH_PARAMETER_SCHEMA,
    DATASTORE_REFRESH_RESPONSE_SCHEMA,
    DATASTORE_USAGE_PARAMETER_SCHEMA,
    DATASTORE_USAGE_RESPONSE_SCHEMA,
    DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA,
    DATASTORE_VSAN_FOLDER_LIST_RESPONSE_SCHEMA,
    EVENT_TAIL_PARAMETER_SCHEMA,
    EVENT_TAIL_RESPONSE_SCHEMA,
    FOLDER_CREATE_PARAMETER_SCHEMA,
    FOLDER_CREATE_RESPONSE_SCHEMA,
    FOLDER_DELETE_PARAMETER_SCHEMA,
    FOLDER_DELETE_RESPONSE_SCHEMA,
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
    HOST_MAINTENANCE_EXIT_PARAMETER_SCHEMA,
    HOST_MAINTENANCE_EXIT_RESPONSE_SCHEMA,
    HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
    HOST_SERVICE_CONTROL_RESPONSE_SCHEMA,
    HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA,
    NAMESPACE_CREATE_PARAMETER_SCHEMA,
    NAMESPACE_CREATE_RESPONSE_SCHEMA,
    NAMESPACE_DELETE_PARAMETER_SCHEMA,
    NAMESPACE_DELETE_RESPONSE_SCHEMA,
    NAMESPACE_STATUS_PARAMETER_SCHEMA,
    NAMESPACE_STATUS_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_VLAN_SET_RESPONSE_SCHEMA,
    PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
    PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
    RESOURCE_POOL_CREATE_PARAMETER_SCHEMA,
    RESOURCE_POOL_CREATE_RESPONSE_SCHEMA,
    RESOURCE_POOL_DELETE_PARAMETER_SCHEMA,
    RESOURCE_POOL_DELETE_RESPONSE_SCHEMA,
    STORAGE_POLICY_CREATE_PARAMETER_SCHEMA,
    STORAGE_POLICY_CREATE_RESPONSE_SCHEMA,
    STORAGE_POLICY_DELETE_PARAMETER_SCHEMA,
    STORAGE_POLICY_DELETE_RESPONSE_SCHEMA,
    STORAGE_POLICY_LIST_PARAMETER_SCHEMA,
    STORAGE_POLICY_LIST_RESPONSE_SCHEMA,
    SUPERVISOR_DISABLE_PARAMETER_SCHEMA,
    SUPERVISOR_DISABLE_RESPONSE_SCHEMA,
    SUPERVISOR_ENABLE_PARAMETER_SCHEMA,
    SUPERVISOR_ENABLE_RESPONSE_SCHEMA,
    SUPERVISOR_STATUS_PARAMETER_SCHEMA,
    SUPERVISOR_STATUS_RESPONSE_SCHEMA,
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
    VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA,
    VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA,
    VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA,
    VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA,
    VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA,
    VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA,
    VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA,
    VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
    VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.host_certificate import (
    HOST_CERTIFICATES_GROUP_KEY,
    HOST_CERTIFICATES_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.license_common import (
    LICENSE_GROUP_KEY,
    LICENSE_KEY_TAG,
    LICENSE_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    INSTALL_GROUP_KEY,
    INSTALL_WHEN_TO_USE,
)
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "COMPOSITES_NOT_ON_8_0",
    "register_vmware_composite_operations",
    "register_vmware_composite_operations_8_0",
]


# Natural-key shorthand. Every composite registers against
# ``(product="vmware", version="9.0", impl_id="vmware-rest")`` -- the
# same triple :class:`VmwareRestConnector` advertises -- so the
# dispatcher's ``connector_id="vmware-rest-9.0"`` lookup resolves
# every read composite alongside the ~3,470 ingested ops.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"

#: The second versioned catalog of the same implementation
#: (``connector_id="vmware-rest-8.0"``, served by ``VmwareRest80Connector``).
#: The resolver sends a fingerprinted 8.0.x target there, so the composites
#: are registered there too (#3789). The handlers already pick the right
#: path per vCenter release (VI-JSON for writes REST cannot do, the 8.0.x
#: arms in ``_write.py``), so the same handler serves both catalogs.
_VERSION_8_0 = "8.0"

#: Composites NOT registered on the 8.0 catalog, each with the reason.
#: ``tests/test_connectors_vmware_rest_catalog_parity.py`` checks that every
#: composite on the 9.0 catalog is either on the 8.0 catalog or listed here,
#: so a new composite cannot silently skip the 8.0 catalog.
_SUPERVISOR_9X_REASON = (
    "built and checked against the vSphere 9.x Supervisor and namespace API "
    "(the 9.x enable path, the 9.x network type enums and the namespaces v2 "
    "instances API); the vCenter 8.0 shape of this API is not checked yet"
)
COMPOSITES_NOT_ON_8_0: dict[str, str] = {
    "vmware.composite.supervisor.enable": _SUPERVISOR_9X_REASON,
    "vmware.composite.supervisor.disable": _SUPERVISOR_9X_REASON,
    "vmware.composite.supervisor.status": _SUPERVISOR_9X_REASON,
    "vmware.composite.namespace.create": _SUPERVISOR_9X_REASON,
    "vmware.composite.namespace.delete": _SUPERVISOR_9X_REASON,
    "vmware.composite.namespace.status": _SUPERVISOR_9X_REASON,
}


#: Curated agent-actionable group selectors for the vmware-rest
#: composite surface (T4b #732). Surfaced verbatim by
#: ``list_operation_groups`` so the LLM client picks the right group
#: before drilling into ``search_operations``. Each text names the
#: group's main reads and its main changes. The same groups also hold
#: the operations ingested from the vSphere API files, and those have
#: their own safety levels. So a text names a safety level only next to a
#: named operation, never for the whole group (#4120;
#: ``tests/test_operation_group_texts.py`` checks it).
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "cluster": (
        "Use for clusters, DRS and resource pools. Read: a cluster's DRS "
        "settings and the moves DRS recommends now "
        "(cluster.drs_recommendations, safe). Changes: "
        "update every host of a cluster in turn (cluster.patch, dangerous); add a "
        "DRS rule that keeps VMs together or apart (cluster.drs_rule.create, "
        "dangerous) or runs a VM group on a host group "
        "(cluster.drs_vm_host_rule.create, caution); delete a DRS rule "
        "(cluster.drs_rule.delete, destructive); create a resource pool "
        "(resource_pool.create, caution) or delete one (resource_pool.delete, "
        "dangerous). Pair with 'host' for one host's maintenance and with 'vm' "
        "to move VMs."
    ),
    "events": (
        "Use for vCenter events: what changed, and when. Reads: the latest "
        "events, filtered by time, event type or one object (event.tail, safe); "
        "for a long history, open an event collector "
        "(POST:/EventManager/{moId}/CreateCollectorForEvents), page through it "
        "(POST:/EventHistoryCollector/{moId}/ReadNextEvents) and destroy it "
        "when done. Changes: write a note of your own into the event log, for "
        "example the start of a maintenance window "
        "(POST:/EventManager/{moId}/LogUserEvent). The right group for "
        "incident triage when you do not know yet which object to look at. "
        "Pair with 'vm' or 'host' once an event names an object."
    ),
    "performance": (
        "Use for the performance counters of one VM, host or cluster. Reads: "
        "the available counters and their recent samples in one call "
        "(performance.summary, safe); the samples of chosen counters "
        "(POST:/PerformanceManager/{moId}/QueryPerf) and the counter "
        "definitions (POST:/PerformanceManager/{moId}/QueryPerfCounter). "
        "Changes: set how long vCenter keeps statistics and how often it "
        "samples (POST:/PerformanceManager/{moId}/UpdatePerfInterval), or how "
        "much detail it collects per counter "
        "(POST:/PerformanceManager/{moId}/UpdateCounterLevelMapping). These two "
        "change the whole vCenter, not one object, and more detail costs "
        "database space. The right group for 'is this VM busy?' or 'how did "
        "CPU look in the last hour?'. Pair with 'vm' or 'host' to get the "
        "object id first."
    ),
    "storage": (
        "Use for datastores, the files on them, and VM storage policies. Reads: "
        "list datastores and read one (GET:/api/vcenter/datastore); capacity, "
        "free space and the VMs on each datastore (datastore.usage, safe); the "
        "files in one datastore folder (datastore.file.list, safe); the folders "
        "of a vSAN datastore with their size and the VMs that use them, or the "
        "files of one such folder (datastore.vsan.folder.list, safe); the "
        "top-level folders of every datastore that no registered VM uses "
        "(datastore.orphan_dirs, safe); make ESXi "
        "read a datastore's capacity and free space again (datastore.refresh, "
        "safe); list storage policies (storage_policy.list, safe). Changes: "
        "create a datastore folder (datastore.dir.create, caution); delete a file "
        "or folder, refused for files of registered VMs (datastore.file.delete, "
        "destructive); create a storage policy (storage_policy.create, caution) "
        "or delete one (storage_policy.delete, destructive). Pair with 'vm' when "
        "the next step acts on one VM."
    ),
    "networking": (
        "Use for networks, distributed switches and port groups. Reads: list "
        "networks (GET:/api/vcenter/network), and distributed port groups with "
        "their switch, VLAN and connected VMs (network.portgroup.audit, safe). "
        "Changes: create a distributed port group with a VLAN trunk or one "
        "access VLAN (network.portgroup.create, dangerous); change its VLAN "
        "(network.portgroup.vlan.set, dangerous) or its security policy "
        "(network.portgroup.security.set, dangerous); delete a distributed port "
        "group (network.portgroup.delete, destructive) or a port group on one "
        "host's standard switch (host.standard_portgroup.delete, destructive). "
        "Both deletes are refused while something uses the port group. Pair "
        "with 'vm' for one VM's NICs, and with 'host' before "
        "host.detach_from_vds."
    ),
    "vm": (
        "Use for virtual machines. Reads: list VMs (GET:/api/vcenter/vm), read "
        "one VM (GET:/api/vcenter/vm/{vm}), and a VM's CPU and memory limits "
        "(vm.resource_allocation.show, safe). To list a VM's snapshots, use "
        "vmware.snapshot.list. Changes: power one VM or many (vm.power, "
        "vm.power.bulk, dangerous); create, clone or deploy a VM (vm.create, "
        "vm.clone, vm.clone_from_template, vm.deploy_from_library, "
        "vm.import_from_library, dangerous); change CPU, memory, disks, NICs or "
        "CD drives (vm.resize, vm.disk.grow, vm.disk.attach, vm.nic.repoint, "
        "vm.cdrom.add, vm.device.cdrom, dangerous); set CPU and memory limits "
        "(vm.resource_allocation.set, caution); unmount the VMware Tools "
        "installer (vm.tools.installer.unmount, caution); move a VM to another "
        "host (vm.migrate, dangerous); take a snapshot (vm.snapshot.create, "
        "caution), revert to one (vm.snapshot.revert, dangerous) or delete one "
        "(vm.snapshot.delete, destructive); delete a powered-off VM and its "
        "disks (vm.destroy, destructive); create a VM folder (folder.create, "
        "dangerous) or delete an empty one (folder.delete, destructive). Pair "
        "with 'storage', 'networking' or 'cluster' for the reads a create or a "
        "move needs."
    ),
    "host": (
        "Use for ESXi hosts. Reads: list hosts (GET:/api/vcenter/host). Changes: "
        "move every running VM off a host, then put it into maintenance mode "
        "(host.evacuate, dangerous); take a host out of maintenance mode "
        "(host.maintenance.exit, dangerous); move a host's VM NICs to a "
        "standard network, then take the host off a distributed switch "
        "(host.detach_from_vds, dangerous); mount an NFS export as a datastore "
        "(host.datastore_mount_nfs, dangerous); mark disks as flash or HDD "
        "(host.disk_mark_flash, dangerous); start, stop or restart an allowed "
        "host service (host.service_control, dangerous). Choose the host by "
        "name or moref. Pair with 'networking' before host.detach_from_vds, and "
        "with 'cluster' or 'vm' for the reads before a change."
    ),
    "guest": (
        "Use for guest OS customization: how a cloned VM gets its host name and "
        "IP address on first boot. Changes: save a named customization spec "
        "with host name, IP per NIC, gateway and DNS, or a Windows sysprep spec "
        "(guest.customization_spec.create, dangerous); apply a saved spec to a "
        "powered-off VM, and power it on if asked (vm.customize, dangerous). "
        "Use it after a 'vm' clone or create. A spec can hold admin passwords; "
        "they never reach a reviewer."
    ),
    "guest_ops": (
        "Use to work inside a running VM's guest OS through VMware Tools, "
        "instead of 'govc guest.run'. Reads: list the guest's processes "
        "(vm.guest.process.list, safe); read its environment variables "
        "(vm.guest.env.read, safe); show its network settings, without a guest "
        "login (vm.guest.net.show, safe); read a file's size and attributes, or "
        "the file itself if asked (vm.guest.file.read, caution). Changes: write "
        "a file into the guest (vm.guest.file.write, dangerous); run a program "
        "in the guest (vm.guest.program.run, dangerous). The guest login comes "
        "from the target's Vault secret (guest_username, guest_password), never "
        "from a parameter. Needs VMware Tools in the guest."
    ),
    "namespace_management": (
        "Use for the vSphere Supervisor (Kubernetes on a cluster) and its "
        "vSphere Namespaces. Reads: the Supervisor state of a cluster "
        "(supervisor.status, safe) and the state of a namespace "
        "(namespace.status, safe), both with a ready flag to poll. Changes: turn "
        "on the Supervisor for one cluster (supervisor.enable, dangerous) or "
        "remove it (supervisor.disable, dangerous); both run in the background, "
        "so poll supervisor.status. Create a namespace with a storage policy, a "
        "Kubernetes release library and VM classes (namespace.create, caution); "
        "delete a namespace and everything in it (namespace.delete, "
        "destructive). Pair with 'storage' for the storage policy id, "
        "'content_library' for the library id, and 'networking' for the network "
        "settings."
    ),
    # Shared with the typed content-library reads and finds: the same key and text.
    CONTENT_LIBRARY_GROUP_KEY: CONTENT_LIBRARY_WHEN_TO_USE,
    # Shared with the two typed licence reads: the same key and text.
    LICENSE_GROUP_KEY: LICENSE_WHEN_TO_USE,
    # Shared with the typed host certificate and TPM reads: the same key and text.
    HOST_CERTIFICATES_GROUP_KEY: HOST_CERTIFICATES_WHEN_TO_USE,
    # #4383: shared with the typed read vmware.alarm.triggered.list, which
    # registers under the same group key with the same text.
    ALARMS_GROUP_KEY: ALARMS_WHEN_TO_USE,
    "migration": MIGRATION_GROUP_WHEN_TO_USE,
    # #4394: shared with the typed read vmware.vcenter.deployment.status.
    INSTALL_GROUP_KEY: INSTALL_WHEN_TO_USE,
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
        op_id=MIGRATION_INVENTORY_OP_ID,
        handler=migration_inventory_composite,
        summary=MIGRATION_INVENTORY_SUMMARY,
        description=MIGRATION_INVENTORY_DESCRIPTION,
        parameter_schema=MIGRATION_INVENTORY_PARAMETER_SCHEMA,
        response_schema=MIGRATION_INVENTORY_RESPONSE_SCHEMA,
        group_key="migration",
        tags=["composite", "read-only", "migration", "inventory", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=MIGRATION_INVENTORY_LLM_INSTRUCTIONS,
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_recommendations",
        handler=cluster_drs_recommendations_composite,
        summary="Read the DRS settings of a cluster and, if asked, the moves DRS recommends now.",
        description=(
            "Reads the cluster summary and its DRS settings (on or off, automation level, "
            "migration threshold) in one call. With include_recommendations_history it also "
            "returns the DRS recommendations that are pending. Read-only."
        ),
        parameter_schema=CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "read-only", "cluster", "drs"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS[
            "vmware.composite.cluster.drs_recommendations"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.event.tail",
        handler=event_tail_composite,
        summary="Read vCenter events, filtered by time, event type or one object.",
        description=(
            "Reads events from the vCenter event log (EventManager.QueryEvents). Optional "
            "filters: a time window, event types and one object. max_count limits the result on "
            "the server. Each event is a short row: time, type, message and the object it is "
            "about. Read-only."
        ),
        parameter_schema=EVENT_TAIL_PARAMETER_SCHEMA,
        response_schema=EVENT_TAIL_RESPONSE_SCHEMA,
        group_key="events",
        tags=["composite", "read-only", "events", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.event.tail"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.performance.summary",
        handler=performance_summary_composite,
        summary="Read the performance counters and recent samples of one VM, host or cluster.",
        description=(
            "Lists the performance counters vCenter has for one object (QueryAvailablePerfMetric) "
            "and reads their recent samples (QueryPerf). Counters are given by number. "
            "max_samples limits the samples per counter for real-time data. Read-only."
        ),
        parameter_schema=PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
        response_schema=PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
        group_key="performance",
        tags=["composite", "read-only", "performance", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.performance.summary"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.usage",
        handler=datastore_usage_composite,
        summary="List datastores with capacity, free space and the VMs on each.",
        description=(
            "Lists the datastores with their capacity, free space and type, and the number and "
            "names of the VMs on each. filter_names keeps only the named datastores. Read-only."
        ),
        parameter_schema=DATASTORE_USAGE_PARAMETER_SCHEMA,
        response_schema=DATASTORE_USAGE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.usage"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.refresh",
        handler=datastore_refresh_composite,
        summary="Make ESXi read a datastore's capacity and free space again, then return them.",
        description=(
            "Asks vSphere to read the datastore's volume again (RefreshDatastore, and "
            "RefreshDatastoreStorageInfo with storage_info), then returns the new summary. Use "
            "it after an NFS export grew. Works on vCenter and on a standalone ESXi host. "
            "Read-only."
        ),
        parameter_schema=DATASTORE_REFRESH_PARAMETER_SCHEMA,
        response_schema=DATASTORE_REFRESH_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.refresh"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.file.list",
        handler=datastore_file_list_composite,
        summary="List the files in one datastore folder, with size and last change.",
        description=(
            "Lists the files and folders in one datastore folder (SearchDatastore_Task), by "
            "datastore name and a path from the root. pattern keeps matching file names. "
            "include_usage adds folder sizes and the VMs that use each entry. Works on "
            "vCenter and on a standalone ESXi host. Read-only."
        ),
        parameter_schema=DATASTORE_FILE_LIST_PARAMETER_SCHEMA,
        response_schema=DATASTORE_FILE_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.file.list"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.vsan.folder.list",
        handler=datastore_vsan_folder_list_composite,
        summary="List the folders of a vSAN datastore, or the files in one folder.",
        description=(
            "Lists the top-level folders of a vSAN datastore with friendly name, UUID path, "
            "size and the registered VMs that use each. With folder, lists the files in that "
            "folder with size and last change; a missing folder answers 'absent'. Read-only."
        ),
        parameter_schema=DATASTORE_VSAN_FOLDER_LIST_PARAMETER_SCHEMA,
        response_schema=DATASTORE_VSAN_FOLDER_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "vsan", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.datastore.vsan.folder.list"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.orphan_dirs",
        handler=datastore_orphan_dirs_composite,
        summary="Find the top-level datastore folders that no registered VM uses.",
        description=(
            "Checks every datastore of a vCenter (or the named ones) in one call. One row per "
            "top-level folder with size, file count, latest change, the VMs that use it and a "
            "classification: orphan, system or claimed. Datastores that cannot be read are "
            "listed in skipped. vCenter only. Read-only."
        ),
        parameter_schema=DATASTORE_ORPHAN_DIRS_PARAMETER_SCHEMA,
        response_schema=DATASTORE_ORPHAN_DIRS_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "datastore", "cleanup", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.orphan_dirs"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.audit",
        handler=network_portgroup_audit_composite,
        summary="List distributed portgroups with their switch, VLAN and VMs.",
        description=(
            "Lists the distributed portgroups. Each row has the parent switch, the VLAN (access "
            "id or trunk ranges) and the VMs with a network adapter on the portgroup. "
            "filter_dvs keeps one switch. Read-only."
        ),
        parameter_schema=NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "read-only", "networking", "portgroup"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.audit"
        ],
    ),
    # ----------------------------------------------------------------
    # Write composites (T6 / #509) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.create",
        handler=vm_create_composite,
        summary="Create a VM with NIC attach, optional VHV enable + optional power-on.",
        description=(
            "Creates a new VM in a folder, adds its NICs and optional data disks, "
            "and can turn on nested virtualization and power it on. If a step after "
            "the create fails, MEHO tries to remove the VM again; rollback.outcome "
            "says whether it did. Like 'govc vm.create'."
        ),
        parameter_schema=VM_CREATE_PARAMETER_SCHEMA,
        response_schema=VM_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.clone",
        handler=vm_clone_composite,
        summary="Clone a VM from a content-library template (synchronous deploy).",
        description=(
            "Makes a new VM from a VM template item in a content library. The "
            "deploy is synchronous, so the result carries the new VM id. Like 'govc "
            "vm.clone'."
        ),
        parameter_schema=VM_CLONE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.clone"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.deploy_from_library",
        handler=vm_deploy_from_library_composite,
        summary="Deploy an OVF/OVA content-library item to a new VM (retires govc library.deploy).",
        description=(
            "Deploys an OVF/OVA item from a content library as a new VM or vApp, "
            "found by item id or by name. Name lookups that match no item or "
            "several are refused before anything changes. Like 'govc "
            "library.deploy'."
        ),
        parameter_schema=VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA,
        response_schema=VM_DEPLOY_FROM_LIBRARY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "ovf"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.deploy_from_library"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.import_from_library",
        handler=vm_import_from_library_composite,
        summary="Import an OVF/OVA content-library item to a new VM via a typed HttpNfcLease.",
        description=(
            "Imports a large OVF/OVA item from a content library as a new VM: MEHO "
            "streams the disks to vCenter itself, so a long copy does not time out. "
            "Works on vCenter 8 and 9. A failed import is cancelled."
        ),
        parameter_schema=VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
        response_schema=VM_IMPORT_FROM_LIBRARY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "ovf"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.import_from_library"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.snapshot.revert",
        handler=vm_snapshot_revert_composite,
        summary="Revert a VM to a snapshot, by name or id; reject on name ambiguity.",
        description=(
            "Puts a VM back to a snapshot, given by name or by id. Everything since "
            "that snapshot is lost. If two snapshots share the name, nothing "
            "changes; pass the id. Like 'govc snapshot.revert'."
        ),
        parameter_schema=VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.revert"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.snapshot.create",
        handler=vm_snapshot_create_composite,
        summary="Take a snapshot of one VM: name, description, with memory or not, quiesce or not.",
        description=(
            "Takes a snapshot of one VM and waits for the vCenter task. Returns the new "
            "snapshot id. Disks only unless memory is set. When the VM already has a "
            "snapshot with that name, nothing is created. Like 'govc snapshot.create'."
        ),
        parameter_schema=VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.snapshot.delete",
        handler=vm_snapshot_delete_composite,
        summary="Delete one VM snapshot by id; its child snapshots stay unless asked.",
        description=(
            "Deletes one snapshot of a VM, by id. The VM keeps its present state; the "
            "restore point is gone for good. Child snapshots stay unless remove_children "
            "is set. A second person always approves, after a preview. Like 'govc "
            "snapshot.remove'."
        ),
        parameter_schema=VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot", "vi-json"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.destroy",
        handler=vm_destroy_composite,
        summary="Permanently destroy (delete) a powered-off VM and all its disks.",
        description=(
            "Deletes a powered-off VM and all its disks for good. A VM that is not "
            "powered off is refused. Destructive: a second person must always "
            "approve it, after seeing the VM, its disks, NICs and snapshots. The "
            "only exception is a standing delete grant on an instance in sandbox "
            "mode. Like 'govc vm.destroy'."
        ),
        parameter_schema=VM_DESTROY_PARAMETER_SCHEMA,
        response_schema=VM_DESTROY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.destroy"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.migrate",
        handler=vm_migrate_composite,
        summary="Migrate a VM via DRS recommendation or explicit target host.",
        description=(
            "Moves a VM to another host, where DRS recommends or to a host you "
            "name. Without a DRS recommendation and without target_host it changes "
            "nothing. Like 'govc vm.migrate'."
        ),
        parameter_schema=VM_MIGRATE_PARAMETER_SCHEMA,
        response_schema=VM_MIGRATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "drs"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.migrate"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.power.bulk",
        handler=vm_power_bulk_composite,
        summary="Apply a power action to every VM matching a filter; aggregate results.",
        description=(
            "Runs one power action (start, stop, suspend, reset) on every VM that "
            "matches a filter and reports each VM's result. One failure does not "
            "stop the rest unless fail_fast is set."
        ),
        parameter_schema=VM_POWER_BULK_PARAMETER_SCHEMA,
        response_schema=VM_POWER_BULK_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "bulk"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.power.bulk"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.power",
        handler=vm_power_composite,
        summary="Apply one power verb to a single VM, including a Tools soft shutdown.",
        description=(
            "Changes the power state of one VM: on, off, reset, or a clean guest "
            "shutdown or reboot through VMware Tools. A guest verb without running "
            "Tools returns tools_unavailable."
        ),
        parameter_schema=VM_POWER_PARAMETER_SCHEMA,
        response_schema=VM_POWER_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "power"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.power"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.disk.grow",
        handler=vm_disk_grow_composite,
        summary="Grow a VM's virtual disk to a larger capacity via ReconfigVM_Task.",
        description=(
            "Makes one virtual disk of a VM larger. A smaller or equal size is "
            "refused before any change. The guest must still extend its file "
            "system. Like 'govc vm.disk.change -size'."
        ),
        parameter_schema=VM_DISK_GROW_PARAMETER_SCHEMA,
        response_schema=VM_DISK_GROW_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "disk", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.disk.grow"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.disk.attach",
        handler=vm_disk_attach_composite,
        summary="Attach an existing VMDK to a VM at an explicit SCSI controller/unit address.",
        description=(
            "Attaches an existing VMDK file to a VM at a given SCSI controller and "
            "unit, for example the shared disk of a cluster. It checks the path, "
            "the controller and that the unit is free first. It never creates a "
            "disk."
        ),
        parameter_schema=VM_DISK_ATTACH_PARAMETER_SCHEMA,
        response_schema=VM_DISK_ATTACH_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "disk", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.disk.attach"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.clone_from_template",
        handler=vm_clone_from_template_composite,
        summary="Clone a folder VM template via CloneVM_Task, with optional inline GOSC.",
        description=(
            "Clones a template VM that lives in a VM folder into a new VM, with "
            "optional host, power-on and a saved guest customization spec applied "
            "in the same step. Refuses a source that is not a template."
        ),
        parameter_schema=VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.clone_from_template"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.evacuate",
        handler=host_evacuate_composite,
        summary="Move every running VM off a host, then put the host into maintenance mode.",
        description=(
            "With DRS fully automated, vCenter moves the running VMs as part of the "
            "maintenance-mode request. Otherwise MEHO moves each running VM where DRS recommends "
            "(vm.migrate) and stops before maintenance mode if one cannot move, unless "
            "tolerate_partial_failure is set. vsan_mode sets the vSAN data option."
        ),
        parameter_schema=HOST_EVACUATE_PARAMETER_SCHEMA,
        response_schema=HOST_EVACUATE_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "maintenance", "recursive"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.evacuate"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.maintenance.exit",
        handler=host_maintenance_exit_composite,
        summary="Take a host out of maintenance mode, after a second person approves.",
        description=(
            "Reads the host first: a host that is not connected is refused, and a host that "
            "is not in maintenance mode answers unchanged with nothing sent. Then sends the "
            "exit request and waits for its task. Returns the host's maintenance state, "
            "connection state and vSphere HA state afterwards."
        ),
        parameter_schema=HOST_MAINTENANCE_EXIT_PARAMETER_SCHEMA,
        response_schema=HOST_MAINTENANCE_EXIT_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "maintenance", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.maintenance.exit"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.detach_from_vds",
        handler=host_detach_from_vds_composite,
        summary="Move the VM NICs on a host to a standard network, then take the host off a DVS.",
        description=(
            "Moves every NIC of every VM on the host to fallback_network, then removes the host "
            "from the distributed switch. If a VM could not be moved, the host stays on the "
            "switch (status incomplete)."
        ),
        parameter_schema=HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA,
        response_schema=HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "networking"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.detach_from_vds"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.create",
        handler=network_portgroup_create_composite,
        summary="Create a distributed portgroup on a switch, with a VLAN trunk or one access VLAN.",
        description=(
            "Creates a distributed portgroup on an existing distributed switch (vSphere "
            "CreateDVPortgroup_Task), waits for the task and reads the new portgroup back. Give "
            "vlan_trunk_ranges or vlan_id, not both; give neither to keep the switch default."
        ),
        parameter_schema=NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.create"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.security.set",
        handler=network_portgroup_security_set_composite,
        summary="Set promiscuous mode, forged transmits and MAC changes on a portgroup.",
        description=(
            "Sets promiscuous mode, forged transmits and MAC address changes on a distributed "
            "portgroup (vSphere ReconfigureDVPortgroup_Task). Only the values you give change. "
            "It reads the policy before and after, so the approver sees both."
        ),
        parameter_schema=NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.security.set"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.vlan.set",
        handler=network_portgroup_vlan_set_composite,
        summary="Change the VLAN of a distributed portgroup: trunk ranges or one access VLAN.",
        description=(
            "Sets the VLAN of an existing distributed portgroup (vSphere "
            "ReconfigureDVPortgroup_Task). The new VLAN replaces the old one; there is no "
            "merge. Give exactly one of vlan_trunk_ranges or vlan_id. When the VLAN already "
            "matches, nothing is written."
        ),
        parameter_schema=NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_VLAN_SET_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.vlan.set"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.patch",
        handler=cluster_patch_composite,
        summary="Update every host of a cluster in turn: maintenance mode, apply image, exit.",
        description=(
            "Updates the hosts of a cluster one at a time: maintenance mode on, apply the host's "
            "vSphere Lifecycle Manager image, maintenance mode off. The first failure stops the "
            "run and lists the hosts not yet done."
        ),
        parameter_schema=CLUSTER_PATCH_PARAMETER_SCHEMA,
        response_schema=CLUSTER_PATCH_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "patch", "long-running"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.patch"],
    ),
    # ----------------------------------------------------------------
    # vim cluster / inventory writes (#2895) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_rule.create",
        handler=cluster_drs_rule_create_composite,
        summary="Add a DRS rule that keeps listed VMs together or apart.",
        description=(
            "Adds a DRS affinity rule (keep the VMs on one host) or anti-affinity rule (keep them "
            "on different hosts) for VMs of a cluster, given by name. A rule name that already "
            "exists changes nothing. Like 'govc cluster.rule.create'."
        ),
        parameter_schema=CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.drs_rule.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_vm_host_rule.create",
        handler=cluster_drs_vm_host_rule_create_composite,
        summary="Add a DRS rule that runs a VM group on, or away from, a host group.",
        description=(
            "Creates a VM group, a host group and a DRS rule that keeps those VMs on those hosts, "
            "or away from them, in one cluster change. With use_existing_groups it uses two "
            "groups that exist and creates only the rule. A name already taken changes nothing."
        ),
        parameter_schema=CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS[
            "vmware.composite.cluster.drs_vm_host_rule.create"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_rule.delete",
        handler=cluster_drs_rule_delete_composite,
        summary="Delete one DRS rule from a cluster, by name. Any rule type.",
        description=(
            "Deletes one DRS rule of any type from a cluster, found by name. VM and host groups "
            "stay. No match or several matches change nothing. Destructive: a second person must "
            "always approve it, after seeing the rule. Like 'govc cluster.rule.remove'."
        ),
        parameter_schema=CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_RULE_DELETE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json", "teardown", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.drs_rule.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_group.members.set",
        handler=cluster_drs_group_members_set_composite,
        summary="Add VMs or hosts to, or remove them from, an existing DRS group.",
        description=(
            "Adds VMs to, or removes VMs from, an existing DRS VM group, by name. Does the same "
            "for hosts in a host group. The approval card shows what changes and which rules use "
            "the group. Like 'govc cluster.group.change'."
        ),
        parameter_schema=CLUSTER_DRS_GROUP_MEMBERS_SET_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_GROUP_MEMBERS_SET_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS[
            "vmware.composite.cluster.drs_group.members.set"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.cluster.drs_group.delete",
        handler=cluster_drs_group_delete_composite,
        summary="Delete one DRS VM group or host group from a cluster, by name.",
        description=(
            "Deletes one DRS VM group or host group, found by name. The VMs and hosts stay. "
            "Refused while a rule still uses the group. Destructive: a second person must always "
            "approve it. Like 'govc cluster.group.remove'."
        ),
        parameter_schema=CLUSTER_DRS_GROUP_DELETE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_GROUP_DELETE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json", "teardown", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.drs_group.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.resource_pool.create",
        handler=resource_pool_create_composite,
        summary="Create a resource pool under a parent pool or a cluster's root pool.",
        description=(
            "Creates a resource pool inside a parent pool, or at the top of a cluster, with "
            "optional CPU and memory reservations, limits and shares. Returns the new pool moid "
            "and checks that it lists under the parent. Like 'govc pool.create'."
        ),
        parameter_schema=RESOURCE_POOL_CREATE_PARAMETER_SCHEMA,
        response_schema=RESOURCE_POOL_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "resource-pool"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.resource_pool.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.resource_pool.delete",
        handler=resource_pool_delete_composite,
        summary="Delete a resource pool; refuse a non-empty pool unless force.",
        description=(
            "Deletes a resource pool. vCenter moves its child pools and VMs up to the parent "
            "pool; they are not deleted. A pool that is not empty is refused unless force is "
            "true. Like 'govc pool.destroy'."
        ),
        parameter_schema=RESOURCE_POOL_DELETE_PARAMETER_SCHEMA,
        response_schema=RESOURCE_POOL_DELETE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "resource-pool"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.resource_pool.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.folder.create",
        handler=folder_create_composite,
        summary="Create a VM folder under a parent folder (synchronous vim CreateFolder).",
        description=(
            "Creates a VM folder under a parent VM folder, given by name or by "
            "moid. A parent name that matches no folder or several is refused "
            "before anything changes. Like 'govc folder.create'."
        ),
        parameter_schema=FOLDER_CREATE_PARAMETER_SCHEMA,
        response_schema=FOLDER_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "inventory", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.folder.create"],
    ),
    # ----------------------------------------------------------------
    # Hardware write composites (#2891) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.resize",
        handler=vm_resize_composite,
        summary="Reconfigure a VM's CPU count / cores-per-socket and/or memory.",
        description=(
            "Changes a VM's vCPU count, cores per socket and/or memory. A change a "
            "running VM cannot take returns requires_power_off; a request that "
            "matches today returns no_change."
        ),
        parameter_schema=VM_RESIZE_PARAMETER_SCHEMA,
        response_schema=VM_RESIZE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.resize"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.nic.repoint",
        handler=vm_nic_repoint_composite,
        summary="Repoint a vNIC onto a distributed or standard portgroup.",
        description=(
            "Moves one VM network adapter to another distributed or standard port "
            "group, found by name or by network moid. A name that matches no "
            "network or several changes nothing. Like 'govc vm.network.change'."
        ),
        parameter_schema=VM_NIC_REPOINT_PARAMETER_SCHEMA,
        response_schema=VM_NIC_REPOINT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "networking"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.nic.repoint"],
    ),
    # ----------------------------------------------------------------
    # VM resource allocation (#3880) -- CPU / memory limit + reservation.
    # No REST expression; both ride the vim seam (the ingested
    # ReconfigVM_Task binding 404s under /api, #3534).
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.resource_allocation.show",
        handler=vm_resource_allocation_show_composite,
        summary="Show a VM's CPU / memory limit, reservation and shares.",
        description=(
            "Shows a VM's CPU and memory limit, reservation and shares (CPU in MHz, "
            "memory in MB; limit -1 means unlimited). Read-only. vmware.vm.info "
            "does not show these fields."
        ),
        parameter_schema=VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA,
        response_schema=VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "read-only", "vm", "resource-allocation", "vi-json"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.resource_allocation.show"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.resource_allocation.set",
        handler=vm_resource_allocation_set_composite,
        summary="Set or clear a VM's CPU / memory limit and reservation.",
        description=(
            "Sets or clears a VM's CPU and memory limit and reservation. Only the "
            "fields you pass change; -1 removes a limit. Bad values are refused "
            "before any change. Takes effect at once and can be undone."
        ),
        parameter_schema=VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA,
        response_schema=VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "resource-allocation", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.resource_allocation.set"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.device.cdrom",
        handler=vm_device_cdrom_composite,
        summary="Remove / update / disconnect a VM CD-ROM device.",
        description=(
            "Removes a VM's CD-ROM drive, changes its backing (for example from a "
            "host-local ISO to CLIENT_DEVICE) or disconnects it. The current "
            "backing is read first, so the approver sees it."
        ),
        parameter_schema=VM_DEVICE_CDROM_PARAMETER_SCHEMA,
        response_schema=VM_DEVICE_CDROM_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.device.cdrom"],
    ),
    # ----------------------------------------------------------------
    # Guest customization (GOSC) composites (#2892) -- dangerous / approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.guest.customization_spec.create",
        handler=guest_customization_spec_create_composite,
        summary="Save a named guest customization spec (host name, IP per NIC, DNS).",
        description=(
            "Saves a named guest customization spec in vCenter for a Linux or Windows guest: "
            "host name, a fixed IP or DHCP per NIC, and DNS. vm.customize or a clone applies "
            "it by name. Windows passwords and keys are used in the spec but never shown in "
            "a preview, broadcast or audit row."
        ),
        parameter_schema=GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA,
        response_schema=GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA,
        group_key="guest",
        tags=["composite", "write", "guest", "customization", "provisioning"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.guest.customization_spec.create"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.customize",
        handler=vm_customize_composite,
        summary="Apply a saved guest customization spec to a VM by name; power it on if asked.",
        description=(
            "Finds the VM by name and sets the saved customization spec on it, so the guest "
            "gets its host name and network settings on the next boot. A powered-on VM is "
            "refused (precondition_failed). With power_on it starts the VM right after."
        ),
        parameter_schema=VM_CUSTOMIZE_PARAMETER_SCHEMA,
        response_schema=VM_CUSTOMIZE_RESPONSE_SCHEMA,
        group_key="guest",
        tags=["composite", "write", "guest", "vm", "customization"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.vm.customize"],
    ),
    # ----------------------------------------------------------------
    # Host-domain write composites (#3182) -- dangerous / requires approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.host.datastore_mount_nfs",
        handler=datastore_mount_nfs_composite,
        summary="Mount an NFS export as a datastore on one ESXi host.",
        description=(
            "Mounts an NFS v3 or v4.1 export as a datastore on one host. Safe to run twice: an "
            "export that is already mounted returns already_mounted, and a name used by another "
            "export returns name_conflict, both without a change."
        ),
        parameter_schema=HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA,
        response_schema=HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "storage", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.datastore_mount_nfs"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.disk_mark_flash",
        handler=disk_mark_flash_composite,
        summary="Mark host disks as flash (SSD) or non-flash (HDD).",
        description=(
            "Marks one or more disks of a host as flash (SSD) or as non-flash (HDD), one task per "
            "disk. One failed disk does not stop the others; each disk gets its own result row. "
            "Like 'govc host.storage.mark -ssd'."
        ),
        parameter_schema=HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA,
        response_schema=HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "storage", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.disk_mark_flash"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.service_control",
        handler=service_control_composite,
        summary="Start, stop or restart an allowed host service; optionally set its start policy.",
        description=(
            "Starts, stops or restarts one ESXi host service and can set when it starts. Only "
            "TSM-SSH, TSM, ntpd and ptpd are allowed; any other service is refused before "
            "anything changes. Like 'govc host.service'."
        ),
        parameter_schema=HOST_SERVICE_CONTROL_PARAMETER_SCHEMA,
        response_schema=HOST_SERVICE_CONTROL_RESPONSE_SCHEMA,
        group_key="host",
        tags=["composite", "write", "host", "service", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.host.service_control"],
    ),
    # ----------------------------------------------------------------
    # Guest-operations channel (#3100 / #3255) -- 3 safe reads + 1 caution
    # read (vm.guest.file.read, #3720) + 2 dangerous writes
    # (vm.guest.file.write / #3100 + vm.guest.program.run / #3255).
    # Guest OS credentials resolve from the target's secret_ref, never
    # from params.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.process.list",
        handler=guest_process_list_composite,
        summary="List the processes running in a VM's guest OS (needs VMware Tools).",
        description=(
            "Lists the processes in the guest OS (ListProcessesInGuest): name, pid, owner, "
            "command line, start time and, for programs started with vm.guest.program.run, "
            "end time and exit code. Uses the guest login from the target's secret. max_processes "
            "limits the list. Read-only."
        ),
        parameter_schema=GUEST_PROCESS_LIST_PARAMETER_SCHEMA,
        response_schema=GUEST_PROCESS_LIST_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "tools"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS[
            "vmware.composite.vm.guest.process.list"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.env.read",
        handler=guest_env_read_composite,
        summary="Read environment variables from a VM's guest OS (needs VMware Tools).",
        description=(
            "Reads the guest user's environment variables as NAME=value strings "
            "(ReadEnvironmentVariableInGuest). Omit names to read all of them. Uses the guest "
            "login from the target's secret. Read-only."
        ),
        parameter_schema=GUEST_ENV_READ_PARAMETER_SCHEMA,
        response_schema=GUEST_ENV_READ_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "tools"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.env.read"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.net.show",
        handler=guest_net_show_composite,
        summary="Show the network settings VMware Tools reports for a guest OS (no guest login).",
        description=(
            "Reads the guest network state VMware Tools reports on the VM: per NIC the IP "
            "addresses, MAC and connected flag (guest.net), and the routes, DNS and gateways "
            "(guest.ipStack). Needs no guest login and runs nothing in the guest. Read-only."
        ),
        parameter_schema=GUEST_NET_SHOW_PARAMETER_SCHEMA,
        response_schema=GUEST_NET_SHOW_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "networking"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.net.show"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.file.read",
        handler=guest_file_read_composite,
        summary="Read a guest file's size and attributes, or (opt-in) the file itself.",
        description=(
            "Starts a file download from the guest OS (InitiateFileTransferFromGuest). By default "
            "it returns size, attributes and a one-time download link. With fetch_content=true "
            "MEHO downloads the file and returns it as text or base64 parts, up to "
            "max_inline_bytes. Needs VMware Tools."
        ),
        parameter_schema=GUEST_FILE_READ_PARAMETER_SCHEMA,
        response_schema=GUEST_FILE_READ_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "file"],
        # Caution, not safe (#3720): fetch_content=true returns arbitrary guest
        # file bytes read as the in-guest login (often privileged) with no
        # allow-list and no redaction, so the op must auto-park for agent /
        # service principals. requires_approval stays False -- caution parks for
        # non-human principals but executes immediately for a human seat, so an
        # operator's diagnostic read is unaffected. The tier is op-level, so even
        # a metadata-only fetch_content=false read parks for agents (the
        # conservative posture chosen in review over per-param tiering).
        safety_level="caution",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.file.read"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.file.write",
        handler=guest_file_write_composite,
        summary="Write a text file into a VM's guest OS (needs approval).",
        description=(
            "Writes UTF-8 text to guest_path in the guest OS: InitiateFileTransferToGuest gives "
            "a one-time upload link, then MEHO uploads the bytes. Uses the guest login from the "
            "target's secret. A second person must approve; the preview shows path and size, "
            "never the content."
        ),
        parameter_schema=GUEST_FILE_WRITE_PARAMETER_SCHEMA,
        response_schema=GUEST_FILE_WRITE_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "write", "guest", "vi-json", "file"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.file.write"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.vm.guest.program.run",
        handler=guest_program_run_composite,
        summary="Run a program in a VM's guest OS (needs approval).",
        description=(
            "Starts a program in the guest OS (StartProgramInGuest) with the guest login from "
            "the target's secret. No output is captured. With wait=true it waits for the exit "
            "code, up to timeout_seconds. A second person must approve. arguments and env are "
            "never shown in the preview, the result or logs."
        ),
        parameter_schema=GUEST_PROGRAM_RUN_PARAMETER_SCHEMA,
        response_schema=GUEST_PROGRAM_RUN_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "write", "guest", "vi-json", "exec"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.program.run"],
    ),
    # ----------------------------------------------------------------
    # namespace-management -- Supervisor (WCP) lifecycle (#3281).
    # 2 dangerous / approval-required writes + 1 safe status read.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.supervisor.enable",
        handler=supervisor_enable_composite,
        summary="Turn on a vSphere Supervisor (Kubernetes control plane) on one cluster.",
        description=(
            "Turns on a vSphere Supervisor on one cluster with the vCenter enable spec "
            "(control_plane, workloads, optional zone). An unknown network type or edge "
            "provider is refused before any change. It returns the new Supervisor id at "
            "once; poll supervisor.status until it is ready."
        ),
        parameter_schema=SUPERVISOR_ENABLE_PARAMETER_SCHEMA,
        response_schema=SUPERVISOR_ENABLE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "write", "namespace-management", "supervisor", "wcp"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.supervisor.enable"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.supervisor.disable",
        handler=supervisor_disable_composite,
        summary="Remove the vSphere Supervisor from a cluster.",
        description=(
            "Removes the Supervisor from a cluster: its control plane VMs and worker nodes "
            "go away, the cluster's networking and zone stay. It returns at once "
            "(status disabling); poll supervisor.status, which shows REMOVING while it runs."
        ),
        parameter_schema=SUPERVISOR_DISABLE_PARAMETER_SCHEMA,
        response_schema=SUPERVISOR_DISABLE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "write", "namespace-management", "supervisor", "wcp"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.supervisor.disable"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.supervisor.status",
        handler=supervisor_status_composite,
        summary="Read the Supervisor state of a cluster (config and Kubernetes status, ready).",
        description=(
            "Reads the Supervisor of a cluster: config_status, kubernetes_status, a ready "
            "flag (RUNNING and READY) and the latest messages and conditions. Use it to poll "
            "after supervisor.enable or supervisor.disable. Read-only."
        ),
        parameter_schema=SUPERVISOR_STATUS_PARAMETER_SCHEMA,
        response_schema=SUPERVISOR_STATUS_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "read-only", "namespace-management", "supervisor", "wcp"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.supervisor.status"],
    ),
    # namespace.* — governed vSphere Namespace create/delete (#3502).
    # The container a VKS guest cluster is created into on an enabled
    # Supervisor; last vCenter-REST step before k8s.apply of the Cluster CR.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.namespace.create",
        handler=namespace_create_composite,
        summary="Create a vSphere Namespace on a ready Supervisor.",
        description=(
            "Creates a vSphere Namespace on a Supervisor, the place a Kubernetes guest "
            "cluster is created in. Optional: who may use it, storage policies, content "
            "libraries and VM classes. vCenter sets it up in the background; poll "
            "namespace.status."
        ),
        parameter_schema=NAMESPACE_CREATE_PARAMETER_SCHEMA,
        response_schema=NAMESPACE_CREATE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "write", "namespace-management", "namespace", "vks", "supervisor"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.namespace.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.namespace.delete",
        handler=namespace_delete_composite,
        summary="Delete a vSphere Namespace and everything in it, then check that it is gone.",
        description=(
            "Deletes a vSphere Namespace by name, then reads it again: deleted, removing "
            "(still being torn down) or still_present. Everything in it is deleted too "
            "(Kubernetes guest clusters, pods, volumes). A second person must always approve."
        ),
        parameter_schema=NAMESPACE_DELETE_PARAMETER_SCHEMA,
        response_schema=NAMESPACE_DELETE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=[
            "composite",
            "write",
            "namespace-management",
            "namespace",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.namespace.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.namespace.status",
        handler=namespace_status_composite,
        summary="Read the state of a vSphere Namespace (exists, config status, ready).",
        description=(
            "Reads a vSphere Namespace: exists, config_status, a ready flag (RUNNING), usage "
            "stats and the latest messages. A namespace that does not exist gives "
            "exists=false, not an error. Use it to poll after namespace.create or "
            "namespace.delete. Read-only."
        ),
        parameter_schema=NAMESPACE_STATUS_PARAMETER_SCHEMA,
        response_schema=NAMESPACE_STATUS_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "read-only", "namespace-management", "namespace", "vks", "supervisor"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.namespace.status"],
    ),
    # storage_policy.* — governed NFS tag-based SPBM policy (#3494)
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.storage_policy.list",
        handler=storage_policy_list_composite,
        summary="List the VM storage policies of vCenter.",
        description=(
            "Reads the VM storage policies of vCenter (GET /vcenter/storage/policies). "
            "policy_ids keeps only the given ids. A long list comes back as a link to the "
            "stored result. Use it to check storage_policy.create and storage_policy.delete. "
            "Read-only."
        ),
        parameter_schema=STORAGE_POLICY_LIST_PARAMETER_SCHEMA,
        response_schema=STORAGE_POLICY_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "policy", "spbm"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.storage_policy.list"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.storage_policy.create",
        handler=storage_policy_create_composite,
        summary="Create a tag-based VM storage policy for NFS datastores.",
        description=(
            "Creates a tag category and a tag, puts the tag on the named datastores, then "
            "creates a VM storage policy that asks for that tag (PBM API). NFS datastores need "
            "this: they have no default policy. Objects that already exist with the same name "
            "are reused."
        ),
        parameter_schema=STORAGE_POLICY_CREATE_PARAMETER_SCHEMA,
        response_schema=STORAGE_POLICY_CREATE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "write", "storage", "policy", "spbm", "tagging", "nfs"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.storage_policy.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.storage_policy.delete",
        handler=storage_policy_delete_composite,
        summary="Delete a VM storage policy by id and check that it is gone.",
        description=(
            "Deletes one VM storage policy (PBM API), then reads the policy list again to check "
            "that it is gone. A policy that is still in use is refused (delete_failed). The "
            "tag and tag category stay. A second person must always approve it."
        ),
        parameter_schema=STORAGE_POLICY_DELETE_PARAMETER_SCHEMA,
        response_schema=STORAGE_POLICY_DELETE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "write", "storage", "policy", "spbm", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.storage_policy.delete"],
    ),
    # Content-library SUBSCRIBED-library composites (#3495) -- the
    # governed TKr/VKr image source for a vSphere Supervisor. Two
    # caution + approval writes (create / sync) and two safe reads
    # (status / items.list). Naming nests under the sibling #3331
    # LOCAL-library family via the ``subscribed`` infix.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.create",
        handler=content_library_subscribed_create_composite,
        summary="Create a subscribed content library, e.g. the Kubernetes release library.",
        description=(
            "Creates a content library on a datastore that copies a published library from "
            "subscription_url, for example the Kubernetes releases a Supervisor uses. It "
            "returns the new library id; vCenter syncs in the background. A BASIC password is "
            "never shown in a preview, broadcast or audit row."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "content-library", "subscribed", "tkr", "vks"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.create"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.sync",
        handler=content_library_subscribed_sync_composite,
        summary="Start a sync of a subscribed content library now.",
        description=(
            "Finds the subscribed library by id or name and tells vCenter to sync it with "
            "the publisher now. It returns as soon as the sync starts; with on_demand only "
            "the item list syncs. Check the result with content_library.subscribed.status."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "content-library", "subscribed", "sync"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.sync"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.status",
        handler=content_library_subscribed_status_composite,
        summary="Read a subscribed content library: its subscription and when it last synced.",
        description=(
            "Finds the subscribed library by id or name and reads its name, subscription "
            "URL, sync settings and last_sync_time. last_sync_time is set once the first "
            "sync is done. vCenter does not return the password. Read-only."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "read-only", "content-library", "subscribed"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.status"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.content_library.subscribed.items.list",
        handler=content_library_subscribed_items_list_composite,
        summary="List the items of a subscribed content library, e.g. Kubernetes releases.",
        description=(
            "Finds the subscribed library by id or name and lists its items: name (for a "
            "Kubernetes release library, the release version), type, version, cached (files "
            "downloaded), size and last sync. A long list comes back as a result handle. "
            "Read-only."
        ),
        parameter_schema=CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "read-only", "content-library", "subscribed", "tkr"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.subscribed.items.list"
        ],
    ),
    # ----------------------------------------------------------------
    # Teardown deletes (#3339; content-library delete = the #3331 delete half)
    # -- the governed inverses of the create-side composites. Destructive tier
    # (+ the caution paired create datastore.dir.create). Each op first reads
    # the object and refuses, before any change, while something still uses it.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.network.portgroup.delete",
        handler=network_portgroup_delete_composite,
        summary="Delete a distributed portgroup. Refused while a VM or adapter uses it.",
        description=(
            "Deletes one distributed portgroup (vSphere Destroy_Task) and reads it again to "
            "check it is gone. Refused before any change while a VM, VMkernel adapter or other "
            "port uses it, or if it is the uplink portgroup or backed by NSX. A second person "
            "must always approve it."
        ),
        parameter_schema=NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA,
        response_schema=NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=["composite", "write", "networking", "vi-json", "teardown", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.network.portgroup.delete"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.host.standard_portgroup.delete",
        handler=host_standard_portgroup_delete_composite,
        summary="Remove a port group from one host's standard switch. Refused while used.",
        description=(
            "Removes one port group from the standard switch of one host (vSphere "
            "RemovePortGroup), then reads the host again. Refused before any change while a VM "
            "on the host (also a powered-off one) or a VMkernel adapter uses it. Other hosts "
            "are not touched. A second person must approve."
        ),
        parameter_schema=HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA,
        response_schema=HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA,
        group_key="networking",
        tags=[
            "composite",
            "write",
            "networking",
            "host",
            "vi-json",
            "teardown",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS[
            "vmware.composite.host.standard_portgroup.delete"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.folder.delete",
        handler=folder_delete_composite,
        summary="Delete an EMPTY inventory folder. Folders with content are refused.",
        description=(
            "Deletes one EMPTY inventory folder. A folder with content is refused "
            "and its content is listed. Root and datacenter folders are refused "
            "too. Destructive: a second person must always approve it."
        ),
        parameter_schema=FOLDER_DELETE_PARAMETER_SCHEMA,
        response_schema=FOLDER_DELETE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=[
            "composite",
            "write",
            "vm",
            "inventory",
            "vi-json",
            "teardown",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.folder.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.file.delete",
        handler=datastore_file_delete_composite,
        summary="Delete a file or folder on a datastore. Refused for files of registered VMs.",
        description=(
            "Deletes one file, or one folder with all it holds, on a VMFS or NFS datastore "
            "(vSphere DeleteDatastoreFile_Task). Refused before any change for unsafe paths and "
            "for files a registered VM uses (listed in blockers). A second person must always "
            "approve it, after seeing the size."
        ),
        parameter_schema=DATASTORE_FILE_DELETE_PARAMETER_SCHEMA,
        response_schema=DATASTORE_FILE_DELETE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=[
            "composite",
            "write",
            "storage",
            "datastore",
            "vi-json",
            "teardown",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.file.delete"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.datastore.dir.create",
        handler=datastore_dir_create_composite,
        summary="Create a folder on a datastore. Does nothing if it already exists.",
        description=(
            "Creates one folder on a VMFS or NFS datastore (vSphere FileManager.MakeDirectory), "
            "then checks that it exists. Same path rules as datastore.file.delete. A folder "
            "that already exists gives unchanged. A missing parent is refused unless "
            "create_parents is true."
        ),
        parameter_schema=DATASTORE_DIR_CREATE_PARAMETER_SCHEMA,
        response_schema=DATASTORE_DIR_CREATE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "write", "storage", "datastore", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.datastore.dir.create"],
    ),
    _CompositeSpec(
        op_id="vmware.composite.content_library.delete",
        handler=content_library_delete_composite,
        summary="Delete a content library (local or subscribed).",
        description=(
            "Deletes one content library, found by id or name. Refused before any change "
            "while another library subscribes to it, a VM has one of its items in a CD-ROM "
            "or floppy drive, or it has items and delete_items is not true. A second person "
            "must always approve."
        ),
        parameter_schema=CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "write", "content-library", "teardown", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.delete"
        ],
    ),
    _CompositeSpec(
        op_id="vmware.composite.content_library.item.delete",
        handler=content_library_item_delete_composite,
        summary="Delete one item of a local content library.",
        description=(
            "Deletes one item of a local content library, found by id or by name in a "
            "library. Refused before any change if the library is not local, or while a VM "
            "has the item in a CD-ROM or floppy drive. A second person must always approve."
        ),
        parameter_schema=CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA,
        response_schema=CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA,
        group_key="content_library",
        tags=["composite", "write", "content-library", "teardown", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.content_library.item.delete"
        ],
    ),
    # Create a local content library and add a file to one from a URL
    # (caution + approval); the rows live in their own modules.
    _CompositeSpec(**CONTENT_LIBRARY_CREATE_COMPOSITE),
    _CompositeSpec(**CONTENT_LIBRARY_ITEM_UPLOAD_COMPOSITE),
    # Add a CD drive and unmount the VMware Tools installer; the rows live in
    # their own module.
    *(_CompositeSpec(**row) for row in VM_CDROM_TOOLS_COMPOSITES),
    # Mount and unmount a content-library ISO on a VM; the rows live in their
    # own module.
    *(_CompositeSpec(**row) for row in VM_CDROM_LIBRARY_ISO_COMPOSITES),
    # ----------------------------------------------------------------
    # Licence write -- dangerous / approval. The full key never leaves
    # the handler; the license-key tag keeps the vCenter bodies out of
    # the flight recorder.
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.license.assign",
        handler=license_assign_composite,
        summary="Assign a licence from the vCenter's licence inventory to one host or the vCenter.",
        description=(
            "Assigns one licence, named by its name or the last 5 characters of its key, "
            "to one asset: a host or the vCenter. MEHO looks up the full key itself and "
            "never shows it. Waits until a second person approves."
        ),
        parameter_schema=LICENSE_ASSIGN_PARAMETER_SCHEMA,
        response_schema=LICENSE_ASSIGN_RESPONSE_SCHEMA,
        group_key=LICENSE_GROUP_KEY,
        tags=["composite", "write", "license", LICENSE_KEY_TAG, "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=LICENSE_ASSIGN_LLM_INSTRUCTIONS,
    ),
    # ----------------------------------------------------------------
    # Alarm changes (#4383) -- caution / approval
    # ----------------------------------------------------------------
    _CompositeSpec(
        op_id="vmware.composite.alarm.acknowledge",
        handler=alarm_acknowledge_composite,
        summary="Acknowledge one triggered vCenter alarm on one object, so its actions stop.",
        description=(
            "Acknowledges one triggered alarm on one object (AlarmManager.AcknowledgeAlarm). "
            "The alarm stays red or yellow, but its actions do not fire again until it was "
            "green once. Reads the alarm before and after. An acknowledged alarm gives "
            "unchanged."
        ),
        parameter_schema=ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA,
        response_schema=ALARM_ACKNOWLEDGE_RESPONSE_SCHEMA,
        group_key=ALARMS_GROUP_KEY,
        tags=["composite", "write", "alarms", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=ALARM_ACKNOWLEDGE_LLM_INSTRUCTIONS,
    ),
    _CompositeSpec(
        op_id="vmware.composite.alarm.reset",
        handler=alarm_reset_composite,
        summary="Reset triggered vCenter alarms to green after their cause is fixed.",
        description=(
            "Resets to green every triggered alarm in the vCenter with the given status, "
            "object kind and trigger kind (ClearTriggeredAlarms); vCenter cannot reset one "
            "alarm alone. This hides a problem until the alarm fires again: fix the cause "
            "first. The answer lists what was cleared."
        ),
        parameter_schema=ALARM_RESET_PARAMETER_SCHEMA,
        response_schema=ALARM_RESET_RESPONSE_SCHEMA,
        group_key=ALARMS_GROUP_KEY,
        tags=["composite", "write", "alarms", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=ALARM_RESET_LLM_INSTRUCTIONS,
    ),
    # Host certificate changes: renew and install dangerous, the signing
    # request caution; all wait for approval. The rows live in their own module.
    *(_CompositeSpec(**row) for row in HOST_CERTIFICATE_COMPOSITES),
    # Install a new vCenter (#4394), dangerous / approval; the rows live in
    # their own module.
    *(_CompositeSpec(**row) for row in VCENTER_INSTALL_COMPOSITES),
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

    Scope: 71 composites total -- 16 read (T5 / #508 + the datastore
    cache-refresh read datastore.refresh / #3789 + the VM allocation read
    vm.resource_allocation.show / #3880 + the 4 guest-ops
    reads / #3100 + the supervisor status + the vSphere Namespace status
    (#3502) / storage-policy list / two SUBSCRIBED content-library reads) +
    55 write (the two caution alarm changes ``alarm.acknowledge`` /
    ``alarm.reset`` / #4383 + the licence assign ``license.assign``, the VM
    snapshot create (caution) + delete (destructive), the seven #3339 teardown
    composites -- six destructive deletes
    + the caution ``datastore.dir.create`` -- + the destructive DRS rule delete
    ``cluster.drs_rule.delete`` + T6 / #509 + the caution VM
    allocation write
    ``vm.resource_allocation.set`` / #3880 + the
    destructive-tier ``vm.destroy`` / #3198, the governed vSphere Namespace
    create/delete ``namespace.create`` (caution) / ``namespace.delete``
    (destructive) / #3502, the governed resource-pool allocation writes
    ``resource_pool.create`` / ``resource_pool.delete`` + the VM-Host
    affinity ``cluster.drs_vm_host_rule.create`` / #3505,
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
    await _register_specs(_COMPOSITES, version=_VERSION, embedding_service=embedding_service)
    register_migration_inventory_dispatch_surface()

    # #3349: publish each write composite's governed child-op set into the
    # discovery registry the GET /api/v1/operations/governed-subops surface
    # reads, so an operator assembles a composite's grant set without reading
    # the ``_SUB_OPS_*`` / ``_VIM_SUB_OPS_*`` manifests in source.
    register_vmware_governed_subops()


async def register_vmware_composite_operations_8_0(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the composites into the ``vmware-rest-8.0`` catalog (#3789).

    A fingerprinted 8.0.x target resolves to the 8.0 catalog, so an agent
    that picks that catalog must find the composites there too. Same rows,
    handlers and policy as the 9.0 catalog, minus
    :data:`COMPOSITES_NOT_ON_8_0`.

    The write steps of a composite are checked under the connector the
    target resolves to (``_step_connector_id`` in ``_write.py``, #4991), so
    on an 8.0.x target a step grant names ``vmware-rest-8.0`` and the
    approval resume of a parked step re-runs the composite on this catalog.
    """
    specs = tuple(spec for spec in _COMPOSITES if spec.op_id not in COMPOSITES_NOT_ON_8_0)
    await _register_specs(specs, version=_VERSION_8_0, embedding_service=embedding_service)
    register_migration_inventory_dispatch_surface()


async def _register_specs(
    specs: tuple[_CompositeSpec, ...],
    *,
    version: str,
    embedding_service: EmbeddingService | None,
) -> None:
    """Upsert *specs* under ``(vmware, version, vmware-rest)``."""
    for spec in specs:
        await register_composite_operation(
            product=_PRODUCT,
            version=version,
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
