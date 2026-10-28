# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pure JSON-Schema-constant declaration module
# for every vmware-rest composite (one parameter + response schema per op);
# the sibling handler modules (_read.py / _write.py) carry the same marker.

"""JSON Schema 2020-12 parameter + response schemas for the 54 vmware-rest composites.

Each schema is the operator-facing input contract; the dispatcher
validates inbound ``params`` against the registered schema before
invoking the handler (see
:func:`meho_backplane.operations._branches.dispatch_composite` and the
:class:`jsonschema.Draft202012Validator` it uses upstream). A malformed
``params`` payload surfaces as an :class:`OperationResult` with
``status="error"`` and the JSON-Schema validator message in
``error`` -- the handler never runs.

Conventions
-----------

* ``additionalProperties=False`` on every schema so a typo on an
  optional key (e.g. ``filter_namse`` instead of ``filter_names``)
  surfaces as a clear validation error rather than silently disappearing
  through a permissive shape.
* Schemas declare only what the handler *reads*. Per-composite
  documentation lives on the schema's ``description`` keys; the meta-
  tools (:mod:`meho_backplane.operations.meta_tools`) surface the
  schema verbatim on ``describe_operation`` calls.
* The 14 read composites are read-only -- the registration call site
  pins ``safety_level="safe"`` and ``requires_approval=False`` on 13 of
  them; ``vm.guest.file.read`` is pinned ``caution`` (#3720), a read
  that can fetch guest bytes as the in-guest login and so auto-parks for
  agent / service principals. The 40 write composites are all
  ``requires_approval=True``: 31 inherit T4's ``safety_level="dangerous"``
  default (G3.1-T6 / #509, single-VM ``vm.power`` / #2301, the mutating
  VI-JSON ``vm.disk.grow`` / #2893, the folder-template
  ``vm.clone_from_template`` / #2894, the vim cluster / inventory
  writes ``cluster.drs_rule.create`` + ``folder.create`` / #2895,
  the #2891 hardware writes ``vm.resize`` / ``vm.nic.repoint`` /
  ``vm.device.cdrom``, the two GOSC composites
  ``guest.customization_spec.create`` / ``vm.customize`` / #2892, and
  the later host-domain / Supervisor / storage-policy / content-library
  families), 6 are ``safety_level="caution"`` (the #3505
  governed-allocation writes ``resource_pool.create`` +
  ``cluster.drs_vm_host_rule.create``, plus ``namespace.create`` /
  ``storage_policy.create`` / ``content_library.subscribed.create`` /
  ``content_library.subscribed.sync``), and 3 are
  ``safety_level="destructive"`` (``vm.destroy`` / #3198 — the first —
  plus ``namespace.delete`` and ``storage_policy.delete``) — the
  governed-delete tier. The schema text reflects which side of that line
  each composite sits on; the registration call site enforces the
  policy.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._guest_perf_response_schemas import (
    GUEST_FILE_ATTRIBUTES_SCHEMA,
    GUEST_NIC_INFO_SCHEMA,
    GUEST_PROCESS_INFO_SCHEMA,
    GUEST_STACK_INFO_SCHEMA,
    PERF_ENTITY_METRIC_SCHEMA,
    PERF_METRIC_ID_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.snapshot_tree import SNAPSHOT_ROW_SCHEMA

__all__ = [
    "CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA",
    "CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA",
    "CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA",
    "CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA",
    "CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA",
    "CLUSTER_DRS_RULE_DELETE_RESPONSE_SCHEMA",
    "CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA",
    "CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA",
    "CLUSTER_MOID_PATTERN",
    "CLUSTER_PATCH_PARAMETER_SCHEMA",
    "CLUSTER_PATCH_RESPONSE_SCHEMA",
    "CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA",
    "CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA",
    "CONTENT_LIBRARY_ID_PATTERN",
    "CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA",
    "CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA",
    "DATACENTER_MOID_PATTERN",
    "DATASTORE_DIR_CREATE_PARAMETER_SCHEMA",
    "DATASTORE_DIR_CREATE_RESPONSE_SCHEMA",
    "DATASTORE_FILE_DELETE_PARAMETER_SCHEMA",
    "DATASTORE_FILE_DELETE_RESPONSE_SCHEMA",
    "DATASTORE_MOID_PATTERN",
    "DATASTORE_PATH_PATTERN",
    "DATASTORE_REFRESH_PARAMETER_SCHEMA",
    "DATASTORE_REFRESH_RESPONSE_SCHEMA",
    "DATASTORE_USAGE_MAX_VM_NAMES",
    "DATASTORE_USAGE_PARAMETER_SCHEMA",
    "DATASTORE_USAGE_RESPONSE_SCHEMA",
    "EVENT_TAIL_PARAMETER_SCHEMA",
    "EVENT_TAIL_RESPONSE_SCHEMA",
    "FOLDER_CREATE_PARAMETER_SCHEMA",
    "FOLDER_CREATE_RESPONSE_SCHEMA",
    "FOLDER_DELETE_PARAMETER_SCHEMA",
    "FOLDER_DELETE_RESPONSE_SCHEMA",
    "FOLDER_MOID_PATTERN",
    "GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA",
    "GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA",
    "GUEST_ENV_READ_PARAMETER_SCHEMA",
    "GUEST_ENV_READ_RESPONSE_SCHEMA",
    "GUEST_FILE_READ_PARAMETER_SCHEMA",
    "GUEST_FILE_READ_RESPONSE_SCHEMA",
    "GUEST_FILE_WRITE_PARAMETER_SCHEMA",
    "GUEST_FILE_WRITE_RESPONSE_SCHEMA",
    "GUEST_NET_SHOW_PARAMETER_SCHEMA",
    "GUEST_NET_SHOW_RESPONSE_SCHEMA",
    "GUEST_PROCESS_LIST_PARAMETER_SCHEMA",
    "GUEST_PROCESS_LIST_RESPONSE_SCHEMA",
    "HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA",
    "HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA",
    "HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA",
    "HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA",
    "HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA",
    "HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA",
    "HOST_EVACUATE_PARAMETER_SCHEMA",
    "HOST_EVACUATE_RESPONSE_SCHEMA",
    "HOST_MAINTENANCE_EXIT_PARAMETER_SCHEMA",
    "HOST_MAINTENANCE_EXIT_RESPONSE_SCHEMA",
    "HOST_MOID_PATTERN",
    "HOST_SERVICE_CONTROL_PARAMETER_SCHEMA",
    "HOST_SERVICE_CONTROL_RESPONSE_SCHEMA",
    "HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA",
    "HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA",
    "NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA",
    "NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA",
    "PERFORMANCE_SUMMARY_PARAMETER_SCHEMA",
    "PERFORMANCE_SUMMARY_RESPONSE_SCHEMA",
    "PORTGROUP_MOID_PATTERN",
    "RESOURCE_POOL_CREATE_PARAMETER_SCHEMA",
    "RESOURCE_POOL_CREATE_RESPONSE_SCHEMA",
    "RESOURCE_POOL_DELETE_PARAMETER_SCHEMA",
    "RESOURCE_POOL_DELETE_RESPONSE_SCHEMA",
    "SUPERVISOR_DISABLE_PARAMETER_SCHEMA",
    "SUPERVISOR_DISABLE_RESPONSE_SCHEMA",
    "SUPERVISOR_ENABLE_PARAMETER_SCHEMA",
    "SUPERVISOR_ENABLE_RESPONSE_SCHEMA",
    "SUPERVISOR_STATUS_PARAMETER_SCHEMA",
    "SUPERVISOR_STATUS_RESPONSE_SCHEMA",
    "VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA",
    "VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA",
    "VM_CLONE_PARAMETER_SCHEMA",
    "VM_CLONE_RESPONSE_SCHEMA",
    "VM_CREATE_PARAMETER_SCHEMA",
    "VM_CREATE_RESPONSE_SCHEMA",
    "VM_CUSTOMIZE_PARAMETER_SCHEMA",
    "VM_CUSTOMIZE_RESPONSE_SCHEMA",
    "VM_DESTROY_PARAMETER_SCHEMA",
    "VM_DESTROY_RESPONSE_SCHEMA",
    "VM_DEVICE_CDROM_PARAMETER_SCHEMA",
    "VM_DEVICE_CDROM_RESPONSE_SCHEMA",
    "VM_DISK_GROW_PARAMETER_SCHEMA",
    "VM_DISK_GROW_RESPONSE_SCHEMA",
    "VM_FOLDER_MOID_PATTERN",
    "VM_MIGRATE_PARAMETER_SCHEMA",
    "VM_MIGRATE_RESPONSE_SCHEMA",
    "VM_MOID_PATTERN",
    "VM_NIC_REPOINT_PARAMETER_SCHEMA",
    "VM_NIC_REPOINT_RESPONSE_SCHEMA",
    "VM_POWER_BULK_PARAMETER_SCHEMA",
    "VM_POWER_BULK_RESPONSE_SCHEMA",
    "VM_POWER_PARAMETER_SCHEMA",
    "VM_POWER_RESPONSE_SCHEMA",
    "VM_RESIZE_PARAMETER_SCHEMA",
    "VM_RESIZE_RESPONSE_SCHEMA",
    "VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA",
    "VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA",
    "VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA",
    "VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA",
    "VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA",
    "VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA",
    "VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA",
    "VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA",
    "VM_SNAPSHOT_MOID_PATTERN",
    "VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA",
    "VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA",
]


#: vCenter content-library and item ids are UUIDs. Nothing else is accepted,
#: so an id can never point a REST path at another object.
CONTENT_LIBRARY_ID_PATTERN: str = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


#: The ``issues`` field a write composite adds when it did not complete (#3813).
#: An ``error``-severity entry makes the dispatcher record the call as failed
#: (``status="error"``, audit ``result_status="error"``), not ``ok``.
#: One ``{category, severity, message}`` entry of an ``issues`` list.
_ISSUE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "severity": {"type": "string", "enum": ["error", "warning", "info"]},
        "message": {"type": "string"},
    },
}

_HARD_FAILURE_ISSUES_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": _ISSUE_ITEM_SCHEMA,
    "description": (
        "Present only when the operation did not complete (timeout, rollback, "
        "partial apply, failed sub-step). Each entry is ``{category, severity, "
        "message}``; an ``error`` entry means the call is recorded as failed. "
        "Absent on success and on a soft refusal such as ``not_found``."
    ),
}


#: ``vmware.composite.cluster.drs_recommendations`` parameter schema.
#:
#: Reads cluster summary + DRS state (optionally surfacing the cluster's
#: current DRS recommendation list). The composite dispatches one
#: ``GET:/vcenter/cluster/{cluster}`` plus one vi-json
#: ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx`` (reading the
#: cluster's whole ``configurationEx``, from which it picks ``drsConfig``,
#: and, on request, ``drsRecommendation``) to a single target -- the pinned
#: vcenter.yaml serves no cluster DRS REST resource (#2986).
CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the cluster (e.g. 'domain-c123'). "
                "Required: drives the {cluster} path parameter on both "
                "sub-ops."
            ),
        },
        "include_recommendations_history": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, the handler also reads the cluster's current "
                "DRS recommendation list (the vim ``drsRecommendation`` "
                "property) in the same RetrievePropertiesEx call and "
                "surfaces it as ``recommendations_history``. Read-only "
                "on either setting; the flag widens the property read, "
                "never adds a mutating call."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7", "include_recommendations_history": True}],
}


#: RFC 3339 date-time (``2026-10-19T08:00:00Z``, optional fraction, ``Z`` or
#: ``+hh:mm``). The parameter validator does not check ``format``, so the
#: pattern is what rejects a bad time before it reaches vCenter.
_RFC3339_DATE_TIME_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$"
)

#: ``vmware.composite.event.tail`` parameter schema.
#:
#: Reads vCenter events from EventManager via the vi-json
#: ``POST:/EventManager/{moId}/QueryEvents`` sub-op. Equivalent of
#: ``govc events`` against a vSphere target. The default ``moId`` is
#: the canonical ``EventManager`` singleton. The filter keys map onto the
#: vim ``EventFilterSpec`` the handler always sends.
EVENT_TAIL_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "moId": {
            "type": "string",
            "minLength": 1,
            "default": "EventManager",
            "description": (
                "Managed-object ID of the EventManager singleton. The "
                "vSphere canonical singleton is 'EventManager'; "
                "non-default values target test fixtures or future "
                "per-DC event managers."
            ),
        },
        "begin_time": {
            "type": "string",
            "format": "date-time",
            "pattern": _RFC3339_DATE_TIME_PATTERN,
            "description": (
                "Only events at or after this time (RFC 3339, for example "
                "'2026-10-19T08:00:00Z'). Sent as EventFilterSpec.time.beginTime."
            ),
        },
        "end_time": {
            "type": "string",
            "format": "date-time",
            "pattern": _RFC3339_DATE_TIME_PATTERN,
            "description": (
                "Only events at or before this time (RFC 3339). Sent as "
                "EventFilterSpec.time.endTime."
            ),
        },
        "event_type_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Only events of these types. Use the vim event class name "
                "(for example 'VmPoweredOnEvent') or the eventTypeId of an "
                "EventEx event (for example "
                "'esx.problem.storage.apd.start'). The eventTypeId of each "
                "returned row is in the same form. Sent as "
                "EventFilterSpec.eventTypeId."
            ),
        },
        "entity": {
            "type": "object",
            "properties": {
                "moId": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Managed-object ID, for example 'datastore-12'.",
                },
                "type": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "vim managed entity type, for example 'Datastore', "
                        "'HostSystem', 'VirtualMachine', 'ClusterComputeResource'."
                    ),
                },
                "recursion": {
                    "type": "string",
                    "enum": ["self", "children", "all"],
                    "default": "all",
                    "description": (
                        "Which events to include: 'self' (only this entity), "
                        "'children' (only its children) or 'all' (both)."
                    ),
                },
            },
            "required": ["moId", "type"],
            "additionalProperties": False,
            "description": (
                "Only events about this managed entity. Sent as EventFilterSpec.entity."
            ),
        },
        "max_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 100,
            "description": (
                "Most events to return. Sent to vCenter as "
                "EventFilterSpec.maxCount, so the limit is applied on the "
                "server."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
    "examples": [
        {
            "begin_time": "2026-10-19T08:00:00Z",
            "event_type_ids": ["VmPoweredOnEvent"],
            "entity": {"moId": "vm-42", "type": "VirtualMachine", "recursion": "self"},
            "max_count": 50,
        }
    ],
}


#: ``vmware.composite.performance.summary`` parameter schema.
#:
#: Reads performance counters for one managed entity via the vi-json
#: ``POST:/PerformanceManager/{moId}/QueryPerf`` sub-op (and the
#: companion ``QueryAvailablePerfMetric`` for counter discovery). The
#: canonical PerformanceManager singleton is ``PerfMgr``.
PERFORMANCE_SUMMARY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_moid": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the entity to query metrics for "
                "(e.g. 'vm-1234', 'host-5678', 'domain-c9'). Required: every "
                "QueryPerf call is per-entity. The vim type is read from the "
                "prefix: 'host-' is HostSystem, 'vm-' is VirtualMachine, "
                "'domain-c' is ClusterComputeResource. For any other entity, "
                "set entity_type."
            ),
        },
        "entity_type": {
            "type": "string",
            "enum": [
                "HostSystem",
                "VirtualMachine",
                "ClusterComputeResource",
                "ComputeResource",
                "ResourcePool",
                "VirtualApp",
                "Datastore",
                "Datacenter",
            ],
            "description": (
                "vim type of the entity. Optional: when left out it comes "
                "from the entity_moid prefix. Set it when the prefix is not "
                "one of 'host-', 'vm-' or 'domain-c'."
            ),
        },
        "perf_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "PerfMgr",
            "description": (
                "Managed-object ID of the PerformanceManager singleton. "
                "The vSphere canonical singleton is 'PerfMgr'; override "
                "only for test fixtures."
            ),
        },
        "interval_seconds": {
            "type": "integer",
            "minimum": 1,
            "default": 20,
            "description": (
                "Sample interval (vim intervalId) for both calls. The "
                "default 20 s is the real-time interval of hosts and VMs. "
                "Clusters have no real-time data; use a historical "
                "interval such as 300 for them."
            ),
        },
        "max_samples": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 60,
            "description": (
                "Cap on the number of returned samples per counter. "
                "Sent to QueryPerf as maxSample; vCenter applies it to "
                "real-time data only."
            ),
        },
    },
    "required": ["entity_moid"],
    "additionalProperties": False,
    "examples": [{"entity_moid": "host-21", "interval_seconds": 20, "max_samples": 15}],
}


#: Upper bound on the ``vm_names`` sample carried by a single
#: ``datastore.usage`` row (the response-schema ``maxItems``; the handler
#: slices to it). ``vm_count`` stays exact -- ``vm_names`` is a bounded
#: sample, not the full set. The bound keeps a one-name ``filter_names``
#: result serialising under the dispatcher's 4096-byte JSONFlux threshold,
#: so it passes through inline (unsampled) and a per-datastore Sensor can
#: select ``$.datastores[0].free_space``; an unbounded list on a VM-dense
#: datastore (a vSAN with hundreds of VMs) pushes the row past the
#: threshold, the whole ``{"datastores": [row]}`` collapses to a sampled
#: envelope, and the assertion loses its selector (#2758). 20 leaves a
#: single row well under 4096 bytes even at maximum VM-name length.
#: ``vmware.composite.datastore.refresh`` parameter schema.
#:
#: Re-probes one datastore's backing volume and reads its refreshed
#: ``Datastore.summary`` back. The vim ``Datastore.RefreshDatastore`` (and
#: the deeper ``RefreshDatastoreStorageInfo``) take only the Datastore
#: MoRef, so the only required param is the datastore moid; on a
#: standalone ESXi host that moid is the ``server:/export`` NAS
#: identifier, on vCenter it is a ``datastore-NNN`` moref.
DATASTORE_REFRESH_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "datastore": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Managed-object ID of the datastore to refresh. On vCenter "
                "this is a ``datastore-NNN`` moref (from datastore.usage / "
                "list_targets); on a standalone ESXi host it is the NAS "
                "identifier ``<server>:/<export>`` (e.g. "
                "``nfs.example:/exports/vol``). The vim RefreshDatastore "
                "method is invoked on this object directly."
            ),
        },
        "storage_info": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, also call the deeper "
                "``Datastore.RefreshDatastoreStorageInfo`` (refreshes all "
                "storage information incl. per-VM usage) after "
                "``RefreshDatastore``. Default false runs only the base "
                "free-space + capacity refresh, which is what a grown NFS "
                "export needs to become visible."
            ),
        },
    },
    "required": ["datastore"],
    "additionalProperties": False,
    "examples": [{"datastore": "datastore-17", "storage_info": False}],
}


#: ``vmware.composite.datastore.refresh`` response schema.
DATASTORE_REFRESH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "datastore": {
            "type": "string",
            "description": "The datastore moid that was refreshed (echoed from params).",
        },
        "refreshed": {
            "type": "boolean",
            "description": (
                "Always true when the composite returns normally: the "
                "``Datastore.RefreshDatastore`` call completed. A failure "
                "raises rather than returning ``refreshed=false``."
            ),
        },
        "storage_info_refreshed": {
            "type": "boolean",
            "description": (
                "True when ``storage_info=true`` was requested and "
                "``RefreshDatastoreStorageInfo`` also completed; false "
                "otherwise."
            ),
        },
        "summary": {
            "type": "object",
            "description": (
                "The refreshed ``Datastore.summary``, read back via a bounded "
                "PropertyCollector read after the refresh. ``capacity`` / "
                "``free_space`` are bytes; a field is null when the "
                "``summary`` DataObject omitted it."
            ),
            "properties": {
                "name": {"type": ["string", "null"], "description": "Datastore name."},
                "capacity": {
                    "type": ["integer", "null"],
                    "description": "Total capacity in bytes after the refresh.",
                },
                "free_space": {
                    "type": ["integer", "null"],
                    "description": "Free space in bytes after the refresh.",
                },
                "accessible": {
                    "type": ["boolean", "null"],
                    "description": "Whether the datastore is currently accessible.",
                },
                "type": {
                    "type": ["string", "null"],
                    "description": "Datastore type (e.g. ``NFS``, ``NFS41``, ``VMFS``).",
                },
                "url": {
                    "type": ["string", "null"],
                    "description": "Datastore URL / mount path.",
                },
            },
            "required": ["name", "capacity", "free_space", "accessible", "type", "url"],
            "additionalProperties": True,
        },
    },
    "required": ["datastore", "refreshed", "storage_info_refreshed", "summary"],
}


DATASTORE_USAGE_MAX_VM_NAMES = 20


#: ``vmware.composite.datastore.usage`` parameter schema.
#:
#: Lists datastores with capacity + free + VM placement aggregation.
#: All sub-ops are vCenter REST. ``filter_names`` narrows the
#: aggregation to the supplied datastore names; the
#: ``GET:/vcenter/datastore`` listing forwards the filter to the
#: server-side query.
DATASTORE_USAGE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter_names": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Optional list of datastore names. When supplied, only "
                "datastores whose name appears in this list are surfaced; "
                "the ``GET:/vcenter/datastore`` listing forwards them as "
                "``filter.names`` (exact match, no fuzzy matching). Empty / "
                "absent returns every datastore the operator can see. "
                "For a per-datastore Sensor, pass exactly one name: the "
                "single-row result returns inline (unsampled), so the "
                "assertion can select ``$.datastores[0].free_space`` (bytes) "
                "and threshold it."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
    "examples": [{"filter_names": ["<datastore-name>"]}],
}


#: ``vmware.composite.network.portgroup.audit`` parameter schema.
#:
#: Lists distributed portgroups with parent switch, VLAN and connected-VM
#: aggregation. The listing is vCenter REST; the per-portgroup details are
#: vim PropertyCollector reads (#3802).
NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter_dvs": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Keep only the portgroups whose parent distributed "
                "switch matches this value. Matches the switch "
                "managed-object ID (for example 'dvs-21') or its "
                "display name."
            ),
        },
        "include_disconnected_vms": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, also count VMs that are not powered on. "
                "Default false counts only powered-on VMs. A VM counts "
                "when it has a network adapter on the portgroup, even if "
                "that adapter is disconnected."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
    "examples": [{"filter_dvs": "dvs-31"}],
}


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------
#
# Each response schema captures the aggregated dict the corresponding
# handler in :mod:`_read` returns. Informational (the dispatcher's
# default reducer does not validate outbound payloads against them);
# declared so the meta-tools
# (:mod:`meho_backplane.operations.meta_tools`) can surface the shape on
# ``describe_operation`` calls and so the
# :class:`~meho_backplane.db.models.EndpointDescriptor` row persists a
# non-null ``response_schema`` for parity with the connector_op surface
# (precedent: ``vault.kv.read`` -- the only other typed-op with an
# explicit response schema today).
#
# Sub-payload shapes (``cluster`` summary, ``drs`` config, datastore
# detail, etc.) are intentionally typed as ``"object"`` with no
# ``properties`` enumeration -- the upstream vSphere REST payload shape
# is owned by Broadcom and out of this composite's contract.


#: ``vmware.composite.cluster.drs_recommendations`` response schema.
#:
#: Captures the cluster summary + DRS config aggregation; the
#: ``recommendations_history`` key is optional and appears only when
#: the operator sets ``include_recommendations_history=True``.
CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Cluster name."},
                "resource_pool": {
                    "type": "string",
                    "description": "Moid of the cluster's root resource pool.",
                },
            },
            "description": (
                "Cluster summary from ``GET:/vcenter/cluster/{cluster}`` "
                "(``Vcenter.Cluster.Info``)."
            ),
        },
        "drs": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "Whether DRS is on."},
                "defaultVmBehavior": {
                    "type": "string",
                    "enum": ["manual", "partiallyAutomated", "fullyAutomated"],
                    "description": "DRS automation level (``DrsBehavior_enum``).",
                },
                "vmotionRate": {
                    "type": "integer",
                    "description": "Migration threshold, 1 to 5: DRS only moves rated above it.",
                },
                "enableVmBehaviorOverrides": {
                    "type": "boolean",
                    "description": "Whether a VM may have its own automation level.",
                },
                "option": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Advanced DRS options, as ``{key, value}`` entries.",
                },
            },
            "description": (
                "DRS configuration payload: the ``drsConfig`` field "
                "(``ClusterDrsConfigInfo`` -- the vim API owns the inner "
                "shape) of the cluster's vim ``configurationEx``, read via "
                "``RetrievePropertiesEx``; ``{}`` when the property is "
                "unset on the target."
            ),
        },
        "recommendations_history": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Recommendation key."},
                    "rating": {
                        "type": "integer",
                        "description": "Confidence, 1 (lowest) to 5 (highest).",
                    },
                    "reason": {"type": "string", "description": "Reason code."},
                    "reasonText": {"type": "string", "description": "Reason as text."},
                    "migrationList": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "The moves (``ClusterDrsMigration``): ``vm``, ``source`` and "
                            "``destination`` host, with their loads."
                        ),
                    },
                },
            },
            "description": (
                "Optional list of the cluster's current DRS "
                "recommendations (vim ``drsRecommendation`` rows) when "
                "``include_recommendations_history=True``. Always a "
                "list when present; absent otherwise."
            ),
        },
    },
    "required": ["cluster", "drs"],
}


#: ``vmware.composite.event.tail`` response schema.
EVENT_TAIL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "createdTime": {
                        "type": ["string", "null"],
                        "description": "When the event was logged (RFC 3339).",
                    },
                    "eventTypeId": {
                        "type": ["string", "null"],
                        "description": (
                            "Event type: the eventTypeId of an EventEx / "
                            "ExtendedEvent, else the vim event class name. "
                            "Usable as an event_type_ids filter value."
                        ),
                    },
                    "fullFormattedMessage": {
                        "type": ["string", "null"],
                        "description": "The event text vCenter shows.",
                    },
                    "entity": {
                        "type": ["object", "null"],
                        "properties": {
                            "type": {
                                "type": ["string", "null"],
                                "description": "vim type, e.g. 'VirtualMachine'.",
                            },
                            "moId": {
                                "type": ["string", "null"],
                                "description": "Moid, e.g. 'vm-42'.",
                            },
                            "name": {
                                "type": ["string", "null"],
                                "description": "Name of the object when the event gives it.",
                            },
                        },
                        "description": (
                            "The managed entity the event is about (the most "
                            "specific one it names), or null."
                        ),
                    },
                },
                "required": ["createdTime", "eventTypeId", "fullFormattedMessage", "entity"],
            },
            "description": (
                "One short row per event from "
                "``POST:/EventManager/{moId}/QueryEvents`` (vi-json), in "
                "the order vCenter returned them. At most ``max_count`` rows."
            ),
        },
        "count": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of rows in ``events``.",
        },
        "moId": {
            "type": "string",
            "description": "EventManager managed-object ID the call targeted.",
        },
        "max_count_applied": {
            "type": "integer",
            "minimum": 1,
            "description": "The ``max_count`` limit sent to vCenter.",
        },
    },
    "required": ["events", "count", "moId", "max_count_applied"],
}


#: ``vmware.composite.performance.summary`` response schema.
PERFORMANCE_SUMMARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_moid": {
            "type": "string",
            "description": "Managed-object ID of the queried entity.",
        },
        "perf_manager_moid": {
            "type": "string",
            "description": "PerformanceManager singleton moid the call targeted.",
        },
        "available_counters": {
            "type": "array",
            "items": PERF_METRIC_ID_SCHEMA,
            "description": (
                "Counters ``QueryAvailablePerfMetric`` lists for the entity "
                "(``PerfMetricId``): counter number and instance."
            ),
        },
        "samples": {
            "type": "array",
            "items": PERF_ENTITY_METRIC_SCHEMA,
            "description": (
                "The ``QueryPerf`` answer (``PerfEntityMetric``): one entry for the entity, "
                "with the sample times and one value series per counter. vCenter applies "
                "``max_samples`` to real-time data."
            ),
        },
        "interval_seconds": {
            "type": "integer",
            "minimum": 1,
            "description": "Sample interval forwarded to QueryPerf.",
        },
        "max_samples_applied": {
            "type": "integer",
            "minimum": 1,
            "description": "Effective ``max_samples`` cap applied to ``samples``.",
        },
    },
    "required": [
        "entity_moid",
        "perf_manager_moid",
        "available_counters",
        "samples",
        "interval_seconds",
        "max_samples_applied",
    ],
}


#: ``vmware.composite.datastore.usage`` response schema.
DATASTORE_USAGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "datastores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Datastore managed-object ID.",
                    },
                    "name": {
                        "type": ["string", "null"],
                        "description": "Datastore name from the listing row.",
                    },
                    "type": {
                        "type": ["string", "null"],
                        "description": "Datastore type (e.g. ``VMFS``, ``NFS``).",
                    },
                    "capacity": {
                        "type": ["integer", "null"],
                        "description": (
                            "Total capacity in bytes; sourced from the per-datastore "
                            "detail payload, falling back to the listing row when the "
                            "detail omits it. ``null`` only when neither carries it."
                        ),
                    },
                    "free_space": {
                        "type": ["integer", "null"],
                        "description": (
                            "Free space in bytes; sourced from the per-datastore detail "
                            "payload, falling back to the listing row when the detail "
                            "omits it. ``null`` only when neither carries it."
                        ),
                    },
                    "vm_count": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "description": (
                            "Number of VMs placed on this datastore; ``null`` when "
                            "the best-effort VM-placement enrichment was skipped or "
                            "discarded (see ``enrichment_note``)."
                        ),
                    },
                    "vm_names": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "maxItems": DATASTORE_USAGE_MAX_VM_NAMES,
                        "description": (
                            "Names of VMs placed on this datastore, bounded to the "
                            "sample size in ``maxItems`` (``vm_count`` is the exact "
                            "total; ``vm_count`` greater than ``len(vm_names)`` means "
                            "the sample was truncated to keep the row inline under "
                            "the JSONFlux byte threshold). ``null`` when the "
                            "best-effort VM-placement enrichment was skipped (see "
                            "``enrichment_note``)."
                        ),
                    },
                    "enrichment_note": {
                        "type": "string",
                        "description": (
                            "Present only when the VM-placement enrichment was "
                            "skipped or discarded. For a failing sub-op it records "
                            "the sub-op, its status, and the underlying error "
                            "(status code + URL where the sub-op carried them); for "
                            "the identical-sets guard it records that the VM set was "
                            "identical across every datastore, so the upstream "
                            "per-datastore filter was not applied."
                        ),
                    },
                },
                "required": ["id", "vm_count", "vm_names"],
            },
            "description": "One row per datastore in scope.",
        },
    },
    "required": ["datastores"],
}


#: ``vmware.composite.network.portgroup.audit`` response schema.
NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "portgroups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Portgroup managed-object ID.",
                    },
                    "name": {
                        "type": ["string", "null"],
                        "description": "Portgroup name from the listing row.",
                    },
                    "dvs": {
                        "type": ["string", "null"],
                        "description": (
                            "Parent distributed switch managed-object ID, "
                            "read from the portgroup's "
                            "``config.distributedVirtualSwitch``. ``null`` "
                            "when vCenter does not report it (for example, "
                            "no read permission on the switch)."
                        ),
                    },
                    "dvs_name": {
                        "type": ["string", "null"],
                        "description": (
                            "Display name of the parent distributed switch. "
                            "``null`` when the switch is unknown."
                        ),
                    },
                    "type": {
                        "type": ["string", "null"],
                        "description": "Portgroup type from the listing row.",
                    },
                    "vlan": {
                        "type": ["object", "null"],
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["access", "trunk"],
                                "description": "One access VLAN, or a trunk of VLAN ranges.",
                            },
                            "vlan_id": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 4094,
                                "description": "The access VLAN ID (mode 'access').",
                            },
                            "ranges": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "start": {
                                            "type": "integer",
                                            "description": "First VLAN id.",
                                        },
                                        "end": {
                                            "type": "integer",
                                            "description": "Last VLAN id (inclusive).",
                                        },
                                    },
                                    "required": ["start", "end"],
                                },
                                "description": (
                                    "The trunk VLAN ranges (mode 'trunk'), "
                                    "sorted, with touching ranges merged."
                                ),
                            },
                        },
                        "required": ["mode"],
                        "description": (
                            "The portgroup's default VLAN, from "
                            "``config.defaultPortConfig.vlan``: "
                            "``{mode: 'access', vlan_id}`` or "
                            "``{mode: 'trunk', ranges}``. ``null`` for a "
                            "private VLAN or a shape vCenter did not report."
                        ),
                    },
                    "vm_count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Number of VMs with a network adapter on this "
                            "portgroup (powered-on only unless "
                            "include_disconnected_vms is true)."
                        ),
                    },
                    "vm_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Names of the VMs with a network adapter on this "
                            "portgroup, from the portgroup's ``vm`` property."
                        ),
                    },
                },
                "required": ["id", "vm_count", "vm_names"],
            },
            "description": "One row per portgroup in scope.",
        },
    },
    "required": ["portgroups"],
}


# ===========================================================================
# Write composites (G3.1-T6 / #509)
# ===========================================================================
#
# The 13 write composites inherit T4's ``safety_level="dangerous"`` +
# ``requires_approval=True`` defaults. The registrar passes those
# explicitly anyway to keep the policy posture obvious at the call site
# alongside the read overrides.


#: ``vmware.composite.vm.create`` parameter schema.
#:
#: Orchestrates folder lookup -> ``POST:/vcenter/vm`` -> per-NIC attach
#: -> optional power-on. Partial-failure rollback removes the half-
#: created VM via ``DELETE:/vcenter/vm/{vm}``. Optional placement pins
#: (``resource_pool`` / ``datastore`` / ``host`` moids, #3096) thread
#: into the CreateSpec ``placement`` alongside the resolved folder moid.
#: On pre-9.0 vCenter (live ``about.version`` major < 9, #3099) the
#: create rides vim ``Folder.CreateVM_Task`` instead — the bare REST
#: create is vendor-defective on 8.0.x — with an always-folded SCSI
#: controller (#3117), disks, NICs and ``nested_hv`` folded into the one
#: ConfigSpec; ``resource_pool`` and ``datastore`` are required on that
#: arm. The optional ``disks`` list (#3117) lands data disks in the same
#: create on both arms. The target folder is spelled as either a
#: display name (``folder_name``, resolved with #3115's
#: datacenter-scoped, fail-loud-on-ambiguity lookup) or an explicit
#: ``folder`` moid pin that skips the lookup.
VM_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folder_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the target VM folder. Resolved via "
                "``GET:/vcenter/folder?filter.names=...`` to the moid "
                "passed to ``POST:/vcenter/vm``. Display names are not "
                "unique across datacenters (every datacenter ships a "
                "default VM folder named ``vm``), so a multi-match "
                "lookup is re-scoped to the placement pins' datacenter "
                "via ``filter.datacenters``; a residual "
                "ambiguity fails with a structured ``rolled_back`` "
                "carrying ``candidate_folders`` — pass ``folder`` to "
                "disambiguate. Ignored when ``folder`` is supplied."
            ),
        },
        "folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional VM folder moid pin, like the other "
                "placement pins. When present the "
                "``folder_name`` display-name lookup is skipped "
                "entirely and this moid rides the CreateSpec "
                "``placement`` (REST arm) / ``CreateVM_Task`` parent "
                "(pre-9.0 vim arm) verbatim; the ``folder_lookup`` "
                "entry is omitted from ``steps_succeeded``. One of "
                "``folder`` / ``folder_name`` is required."
            ),
        },
        "resource_pool": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional ResourcePool moid pin. Threaded into "
                "the CreateSpec ``placement`` alongside the resolved "
                "folder moid. The pinned spec marks ``resource_pool`` as "
                "currently required when neither host nor cluster is "
                "given, so multi-host clusters should pin it; absent, "
                "vCenter's placement defaulting applies. On a pre-9.0 "
                "target it is REQUIRED: vim CreateVM_Task takes "
                "an explicit pool and has no placement defaulting."
            ),
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional Datastore moid pin for the VM home and "
                "disk storage. Threaded into the CreateSpec "
                "``placement``; absent, vCenter's placement defaulting "
                "applies — on hosts with host-local datastores the "
                "default may land the VM on the wrong store. On a "
                "pre-9.0 target it is REQUIRED: the vim arm "
                "resolves its display name into the "
                "``files.vmPathName`` VM home."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional HostSystem moid pin. Threaded into the "
                "CreateSpec ``placement``; per the pinned spec a "
                "``resource_pool`` given alongside it must belong to "
                "that host. Absent, vCenter's placement defaulting "
                "applies."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "VM display name. Required by ``POST:/vcenter/vm``.",
        },
        "guest_os": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Guest-OS identifier (e.g. ``UBUNTU_64``). Drives the "
                "ConfigSpec.guestOS field on ``POST:/vcenter/vm``. On a "
                "pre-9.0 target it is mapped to the vim guestId "
                "(``VMKERNEL_8`` -> ``vmkernel8Guest``); an identifier "
                "outside the curated mapping fails closed with a "
                "structured ``rolled_back`` before any write."
            ),
        },
        "cpu_count": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "Number of virtual CPUs on the ConfigSpec.",
        },
        "memory_mib": {
            "type": "integer",
            "minimum": 64,
            "default": 1024,
            "description": "Memory size in MiB on the ConfigSpec.",
        },
        "nics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "network": {
                        "type": "string",
                        "description": "Network moid the NIC attaches to.",
                    },
                    "backing_type": {
                        "type": "string",
                        "enum": [
                            "STANDARD_PORTGROUP",
                            "DISTRIBUTED_PORTGROUP",
                            "OPAQUE_NETWORK",
                        ],
                        "default": "STANDARD_PORTGROUP",
                        "description": (
                            "``Ethernet.BackingSpec.type`` for the NIC's "
                            "network backing. Defaults to a standard "
                            "portgroup."
                        ),
                    },
                },
                "required": ["network"],
            },
            "default": [],
            "description": (
                "Per-NIC spec. Each entry drives a "
                "``POST:/vcenter/vm/{vm}/hardware/ethernet`` adapter "
                "create (the network rides the ``backing`` spec) after "
                "the VM is created. Empty list creates the VM with no "
                "NICs. On a pre-9.0 target NICs fold into the "
                "CreateVM_Task ConfigSpec as vmxnet3 ``deviceChange`` "
                "adds (distributed or standard portgroup backing; "
                "``OPAQUE_NETWORK`` has no vim expression there and "
                "fails closed)."
            ),
        },
        "disks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "capacity_gb": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Disk capacity in GiB (converted to bytes for the vendor spec)."
                        ),
                    },
                    "provisioning": {
                        "type": "string",
                        "enum": ["thin", "thick", "eagerzeroedthick"],
                        "default": "thin",
                        "description": (
                            "Disk provisioning policy. ``thin`` — "
                            "allocate-on-write (``thinProvisioned=true``); "
                            "``thick`` — lazy-zeroed thick "
                            "(``thinProvisioned=false``, ``eagerlyScrub=false``); "
                            "``eagerzeroedthick`` — eager-zeroed thick "
                            "(``thinProvisioned=false``, ``eagerlyScrub=true``), "
                            "the WSFC/FCI shared-disk and multi-writer "
                            "requirement. The pinned REST ``VmdkCreateSpec`` "
                            "has no provisioning field, so any value other than "
                            "``thin`` routes the whole create through the vim "
                            "``CreateVM_Task`` arm (which needs "
                            "``resource_pool`` + ``datastore`` pins)."
                        ),
                    },
                    "sharing": {
                        "type": "string",
                        "enum": ["none", "multi_writer"],
                        "default": "none",
                        "description": (
                            "Per-disk multi-writer sharing, distinct "
                            "from SCSI bus-sharing. ``multi_writer`` sets the "
                            "backing's ``sharing=sharingMultiWriter`` so several "
                            "VMs may open the same VMDK concurrently — the mode "
                            "for application-managed clustering (e.g. Oracle "
                            "RAC), where the guest coordinates its own locking. "
                            "Use ``scsi_bus_sharing`` (not this) for WSFC/FCI, "
                            "which relies on SCSI-3 persistent reservations on a "
                            "shared bus. Multi-writer has no REST expression, so "
                            "any value other than ``none`` routes the create "
                            "through the vim arm."
                        ),
                    },
                },
                "required": ["capacity_gb"],
                "additionalProperties": False,
            },
            "default": [],
            "description": (
                "Optional data disks to create with the VM. On the "
                "REST arm each entry threads into the CreateSpec ``disks`` "
                "as a SCSI ``new_vmdk`` sized in bytes (vCenter fabricates "
                "the controller). On the pre-9.0 vim arm — where "
                "``CreateVM_Task`` builds the VM verbatim and adds no "
                "controller — the arm ALWAYS folds a "
                "``VirtualLsiLogicSASController`` (so a fresh VM has a "
                "controller for the governed disk-add "
                "``POST:/vcenter/vm/{vm}/hardware/disk`` even when this "
                "list is empty) plus one ``VirtualDisk`` "
                "``fileOperation: create`` per entry, bound to it. "
                "``provisioning`` defaults to ``thin`` (the datastore default "
                "on the REST arm); a non-default ``provisioning`` or "
                "``sharing`` value routes the create through the vim arm. "
                "An empty list adds no disk."
            ),
        },
        "scsi_bus_sharing": {
            "type": "string",
            "enum": ["none", "virtual", "physical"],
            "default": "none",
            "description": (
                "SCSI bus-sharing mode for the VM's SCSI controller, "
                "distinct from per-disk multi-writer. ``none`` — private bus "
                "(``noSharing``); ``virtual`` — bus shared between VMs on the "
                "same host (``virtualSharing``); ``physical`` — bus shared "
                "between VMs on different hosts (``physicalSharing``), the "
                "Windows Server Failover Cluster (WSFC) / SQL FCI requirement "
                "(SCSI-3 persistent reservations). Applies to the controller "
                "the create folds for the data ``disks``; the boot/OS disk is "
                "not part of this fold, so it never lands on a shared bus. "
                "Any value other than ``none`` routes the create through the "
                "vim ``CreateVM_Task`` arm (the REST create fabricates a "
                "non-shared controller with no sharing knob), which requires "
                "``resource_pool`` + ``datastore`` pins."
            ),
        },
        "nested_hv": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, the handler enables nested hardware-assisted "
                "virtualization (VHV) after NIC attach and before any "
                "power-on: a vim ``ReconfigVM_Task`` with "
                "``VirtualMachineConfigSpec.nestedHVEnabled=true`` through "
                "the governed vmomi write seam (version-correct on vCenter "
                "8.0.x and 9.x — the flag has no REST expression), polled "
                "to a terminal state. A failed leg follows the composite's "
                "rollback contract."
            ),
        },
        "power_on_after_create": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, the handler issues "
                "``POST:/vcenter/vm/{vm}/power?action=start`` after "
                "NIC attach (and after the optional ``nested_hv`` "
                "reconfigure). Default false leaves the VM powered-off."
            ),
        },
    },
    "required": ["name", "guest_os"],
    # #3115: the create-target folder is spelled either as a display name
    # (``folder_name``, resolved) or an explicit moid pin (``folder``,
    # verbatim) — at least one must be present. Mirrors the vm.resize
    # at-least-one-of ``anyOf`` shape.
    "anyOf": [
        {"required": ["folder_name"]},
        {"required": ["folder"]},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "name": "<vm-name>",
            "guest_os": "UBUNTU_64",
            "folder_name": "<vm-folder-name>",
            "cpu_count": 2,
            "memory_mib": 4096,
            "nics": [{"network": "dvportgroup-42", "backing_type": "DISTRIBUTED_PORTGROUP"}],
        }
    ],
}


#: ``vmware.composite.vm.clone`` parameter schema.
#:
#: Orchestrates a content-library deploy. The pinned spec's deploy
#: operation is synchronous (its 200 body is the deployed VM id -- no
#: ``vmw-task=true`` variant exists, #2970), so there is no task wait to
#: configure.
VM_CLONE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_vm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Source VM moid. Resolved via ``GET:/vcenter/vm/{vm}`` "
                "to build the CloneSpec before deploy."
            ),
        },
        "target_name": {
            "type": "string",
            "minLength": 1,
            "description": "Display name for the cloned VM.",
        },
        "library_item": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Content-library template item id (a UUID). Rides the deploy path as "
                "``POST:/vcenter/vm-template/library-items/"
                "{templateLibraryItem}?action=deploy``."
            ),
        },
    },
    "required": ["source_vm", "target_name", "library_item"],
    "additionalProperties": False,
    "examples": [
        {
            "source_vm": "vm-42",
            "target_name": "<new-vm-name>",
            "library_item": "00000000-0000-0000-0000-000000000000",
        }
    ],
}


#: ``vmware.composite.vm.clone_from_template`` parameter schema.
#:
#: Clones a **folder VM template** (a marked-as-template VM) via vim
#: ``VirtualMachine.CloneVM_Task`` — the path ``vm.clone`` (content-library
#: only) cannot serve. Placement moids (``folder`` / ``resource_pool`` /
#: ``datastore`` / ``host``) are vim MoRef values the operator resolves from
#: list ops; ``source_template`` is a display **name** resolved to a moid at
#: dispatch time and asserted to be a template before any clone fires.
VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_template": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the source **folder VM template** (a "
                "marked-as-template VM). Resolved to a moid via "
                "``GET:/vcenter/vm?filter.names=`` and asserted to carry "
                "``config.template=true`` (PropertyCollector read) before any "
                "clone is issued — a non-template source is refused with "
                "``status='not_a_template'``, an unknown name with "
                "``'template_not_found'``, an ambiguous name with "
                "``'ambiguous_template'``."
            ),
        },
        "new_vm_name": {
            "type": "string",
            "minLength": 1,
            "description": "Display name for the newly cloned virtual machine.",
        },
        "folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Destination VM ``Folder`` moid (e.g. 'group-v42'). The "
                "``CloneVM_Task`` ``folder`` argument — where the new VM is "
                "placed in the inventory tree."
            ),
        },
        "resource_pool": {
            "type": "string",
            "minLength": 1,
            "description": (
                "``ResourcePool`` moid the clone attaches to (e.g. "
                "'resgroup-8'). Required by vim for a template→VM clone — it "
                "determines the compute resources available to the clone. For "
                "a DRS cluster, pass the cluster's root resource pool moid."
            ),
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "``Datastore`` moid the clone's files are copied to (e.g. "
                "'datastore-15'). Always required — it names where the new "
                "VM lands on physical storage."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional ``HostSystem`` moid to pin the clone onto a specific "
                "host (e.g. 'host-19'). When unset the resource pool (and DRS, "
                "if enabled) place the VM."
            ),
        },
        "power_on": {
            "type": "boolean",
            "default": False,
            "description": (
                "Power the clone on after creation (``CloneSpec.powerOn``). "
                "When a customization spec is applied, the first power-on "
                "completes the guest customization; defaults to false."
            ),
        },
        "customization_spec_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional name of an existing guest OS customization "
                "specification (GOSC, e.g. one created by "
                "``vmware.composite.guest.customization_spec.create``). "
                "Resolved to its full ``CustomizationSpec`` via vim "
                "``CustomizationSpecManager.GetCustomizationSpec`` and applied "
                "inline in the clone (``CloneSpec.customization``), so the "
                "clone yields a customized VM without a separate customize "
                "dispatch. Secret-bearing customization fields are never "
                "echoed onto the approval preview — only the spec name is."
            ),
        },
        "customization_spec_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "CustomizationSpecManager",
            "description": (
                "vim ``CustomizationSpecManager`` singleton moid used to "
                "resolve ``customization_spec_name``. Defaults to the standard "
                "``ServiceContent.customizationSpecManager`` value; overridable "
                "for a deploy whose singleton moid differs (mirrors the "
                "performance composite's ``perf_manager_moid``). Ignored when "
                "``customization_spec_name`` is unset."
            ),
        },
    },
    "required": ["source_template", "new_vm_name", "folder", "resource_pool", "datastore"],
    "additionalProperties": False,
    "examples": [
        {
            "source_template": "<template-name>",
            "new_vm_name": "<new-vm-name>",
            "folder": "group-v42",
            "resource_pool": "resgroup-8",
            "datastore": "datastore-15",
            "power_on": True,
        }
    ],
}


#: vCenter snapshot moid shape, e.g. ``snapshot-12``.
VM_SNAPSHOT_MOID_PATTERN: str = r"^snapshot-[0-9]+$"


#: ``vmware.composite.vm.snapshot.revert`` parameter schema.
#:
#: Idempotent revert by snapshot name or by snapshot id (exactly one).
#: Ambiguity (multiple snapshots share the name) returns
#: ``status='ambiguous'`` rather than guessing; an id that is not in the
#: VM's snapshot tree returns ``status='not_found'``.
VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Target VM moid. Required for snapshot-tree lookup.",
        },
        "snapshot_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the snapshot to revert to. Give snapshot_name or "
                "snapshot_id, not both. Multiple snapshots with the same name return "
                "``status='ambiguous'``; then pass snapshot_id."
            ),
        },
        "snapshot_id": {
            "type": "string",
            "pattern": VM_SNAPSHOT_MOID_PATTERN,
            "description": (
                "Moid of the snapshot to revert to, e.g. 'snapshot-12' (from "
                "vmware.snapshot.list). Give snapshot_name or snapshot_id, not both. It "
                "must be a snapshot of this VM."
            ),
        },
    },
    "required": ["vm"],
    "oneOf": [{"required": ["snapshot_name"]}, {"required": ["snapshot_id"]}],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "snapshot_name": "<snapshot-name>"},
        {"vm": "vm-42", "snapshot_id": "snapshot-12"},
    ],
}


#: vCenter VM moid shape, e.g. ``vm-42``. A VM name does not match.
VM_MOID_PATTERN: str = r"^vm-[0-9]+$"


#: ``vmware.composite.vm.destroy`` parameter schema (#3198).
#:
#: The first governed destructive delete. A single required ``vm`` moid —
#: the destroy takes no ``force`` flag: a powered-on VM is refused
#: (``status='not_powered_off'``), never implicitly powered off, and the
#: mandatory human approval is the confirmation, not a param.
#:
#: ``vm`` must match :data:`VM_MOID_PATTERN` (#3479). A VM name fails schema
#: validation with ``invalid_params`` before the blast-radius preview runs.
#: The composite does not look a name up: two VMs can share a name, so the
#: caller reads the inventory and passes the moid.
VM_DESTROY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "pattern": VM_MOID_PATTERN,
            "description": (
                "vCenter moid of the VM to permanently destroy, like "
                "'vm-42'. Not the VM name: look the moid up with an "
                "inventory read first. The VM must be "
                "POWERED_OFF — a running VM is refused with "
                "``status='not_powered_off'`` (no implicit power-off)."
            ),
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}


#: ``vmware.composite.vm.migrate`` parameter schema.
#:
#: DRS-deferred relocation. No-recommendation path returns
#: ``status='no_recommendation'``. ``target_host`` overrides the DRS
#: lookup.
VM_MIGRATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Source VM moid.",
        },
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Cluster moid the source VM lives in. Required for the DRS recommendation lookup."
            ),
        },
        "target_host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional explicit target-host moid. When supplied, "
                "bypasses the DRS recommendation lookup."
            ),
        },
    },
    "required": ["vm", "cluster"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "cluster": "domain-c7"}],
}


#: ``Vcenter.Vm.Power.State`` values a VM filter may match.
_VM_POWER_STATES: Final[list[str]] = ["POWERED_OFF", "POWERED_ON", "SUSPENDED"]

#: ``vmware.composite.vm.power.bulk`` parameter schema.
#:
#: Resolve filter -> per-VM power action. Partial-failure tolerated
#: by default; ``fail_fast=True`` aborts on the first failure.
VM_POWER_BULK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "object",
            "properties": {
                "vms": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "VM moids, like ``vm-42``.",
                },
                "names": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Exact VM display names.",
                },
                "folders": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Folder moids; matches VMs directly in these folders.",
                },
                "datacenters": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Datacenter moids.",
                },
                "hosts": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Host moids.",
                },
                "clusters": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Cluster moids.",
                },
                "resource_pools": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Resource pool moids.",
                },
                "power_states": {
                    "anyOf": [
                        {"type": "string", "enum": _VM_POWER_STATES},
                        {"type": "array", "items": {"type": "string", "enum": _VM_POWER_STATES}},
                    ],
                    "description": "Power states to match: one state or a list of states.",
                },
            },
            "description": (
                "VM filter sent to ``GET:/vcenter/vm`` as ``filter.<key>`` "
                "query params (``Vcenter.VM.FilterSpec``). Filters are ANDed. "
                "An empty filter matches every VM, so always narrow it."
            ),
            "default": {},
        },
        "action": {
            "type": "string",
            "enum": ["start", "stop", "suspend", "reset"],
            "description": (
                "Per-VM power action. Forwarded to ``POST:/vcenter/vm/{vm}/power?action=<action>``."
            ),
        },
        "fail_fast": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, abort on the first per-VM failure. Default "
                "false collects per-VM results and reports them in "
                "aggregate."
            ),
        },
    },
    "required": ["action"],
    "additionalProperties": False,
    "examples": [{"filter": {"names": ["<vm-name-1>", "<vm-name-2>"]}, "action": "start"}],
}


#: ``vmware.composite.vm.power`` parameter schema.
#:
#: Single-VM power verb. Hard verbs (``on`` / ``off`` / ``reset``) hit
#: ``POST:/vcenter/vm/{vm}/power``; soft verbs (``guest_shutdown`` /
#: ``guest_reboot``) hit ``POST:/vcenter/vm/{vm}/guest/power`` and require
#: running VMware Tools. ``vm`` must match :data:`VM_MOID_PATTERN` (#3479).
VM_POWER_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "pattern": VM_MOID_PATTERN,
            "description": (
                "vCenter moid of the VM to act on, like 'vm-42'. Not the "
                "VM name: look the moid up with an inventory read first."
            ),
        },
        "verb": {
            "type": "string",
            "enum": ["on", "off", "reset", "guest_shutdown", "guest_reboot"],
            "description": (
                "Power verb. ``on`` / ``off`` / ``reset`` are hard "
                "transitions via ``POST:/vcenter/vm/{vm}/power`` "
                "(immediate; ``off`` / ``reset`` may lose in-guest state). "
                "``guest_shutdown`` / ``guest_reboot`` are clean "
                "Tools-mediated transitions via "
                "``POST:/vcenter/vm/{vm}/guest/power`` and fail with "
                "``status='tools_unavailable'`` when VMware Tools is not "
                "running."
            ),
        },
    },
    "required": ["vm", "verb"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "verb": "guest_shutdown"}],
}


#: ``vmware.composite.host.evacuate`` parameter schema.
#:
#: Lists VMs on host, recursively dispatches ``vmware.composite.vm.migrate``
#: per VM, then enters maintenance. ``tolerate_partial_failure=True``
#: allows maintenance-enter with VMs left on host.
#: ``HostMaintenanceSpec.vsanMode.objectAction`` values
#: (``VsanHostDecommissionModeObjectAction_enum`` in the pinned
#: ``vi-json.yaml``). Shared by every maintenance-mode operation that takes
#: the vSAN data option.
VSAN_MAINTENANCE_MODES: Final[list[str]] = [
    "ensureObjectAccessibility",
    "evacuateAllData",
    "noAction",
]

HOST_EVACUATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": "Host moid to evacuate.",
        },
        "tolerate_partial_failure": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, the host enters maintenance mode even if some "
                "running VMs could not be moved (those VMs stay on the host). "
                "Default false stops before maintenance mode on the first VM "
                "that cannot be moved. Has no effect when DRS is fully "
                "automated: vCenter then moves the VMs itself."
            ),
        },
        "vsan_mode": {
            "type": "string",
            "enum": VSAN_MAINTENANCE_MODES,
            "default": "ensureObjectAccessibility",
            "description": (
                "What vSAN does with the host's data when the host enters "
                "maintenance mode. ensureObjectAccessibility (the default, "
                "also vCenter's own default): keep every object reachable and "
                "move only the data that needs it. evacuateAllData: move all "
                "data off the host. noAction: leave the data where it is. "
                "Ignored on a host without vSAN."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "host-21", "vsan_mode": "ensureObjectAccessibility"}],
}


#: ``vmware.composite.host.maintenance.exit`` parameter schema (#4967).
#:
#: One input: the host. The handler reads the host first and sends the
#: exit only when the host is connected and in maintenance mode.
HOST_MAINTENANCE_EXIT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": "Host moid to take out of maintenance mode.",
        },
    },
    "required": ["host"],
    "additionalProperties": False,
    "examples": [{"host": "host-21"}],
}


#: ``vmware.composite.host.detach_from_vds`` parameter schema.
#:
#: Per-VM NIC migration off the DVS, then DVS host-detach. Refuses to
#: detach when any VM still has active NICs on the DVS.
HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": "Host moid to detach from the DVS.",
        },
        "dvs": {
            "type": "string",
            "minLength": 1,
            "description": (
                "DVS moid the host is currently attached to. Required to scope the portgroup query."
            ),
        },
        "fallback_network": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Standard-switch network moid the host's VM NICs are "
                "migrated to before the DVS detach. Required because "
                "the host loses DVS connectivity at step 4."
            ),
        },
    },
    "required": ["host", "dvs", "fallback_network"],
    "additionalProperties": False,
    "examples": [{"host": "host-21", "dvs": "dvs-31", "fallback_network": "network-13"}],
}


#: ``vmware.composite.cluster.patch`` parameter schema.
#:
#: Sequential per-host maintenance + patch + exit. A per-host failure
#: stops the loop; the cluster is left mixed-state for operator review.
CLUSTER_PATCH_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": "Cluster moid.",
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}],
}


#: ``vmware.composite.vm.disk.grow`` parameter schema.
#:
#: Grow-only virtual-disk capacity change via vim ``ReconfigVM_Task`` (the
#: pinned 9.0 REST spec's ``Disk.UpdateSpec`` carries only ``backing`` — no
#: capacity field, so vim is the sole write path). A request ``<=`` the
#: current capacity is refused (``status="invalid_shrink"``) before any
#: write. ``disk`` is the REST disk id (== the vim ``VirtualDevice.key``).
VM_DISK_GROW_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Managed-object ID of the VM owning the disk (e.g. 'vm-42').",
        },
        "disk": {
            "type": ["string", "integer"],
            "minLength": 1,
            "minimum": 0,
            "description": (
                "Virtual disk identifier — the REST "
                "``com.vmware.vcenter.vm.hardware.Disk`` id (a string, e.g. '2000') "
                "or the vim ``VirtualDevice.key`` it is the string form of "
                "(an integer, e.g. 2000). Both forms are accepted. "
                "The handler matches it against the VM's "
                "``config.hardware.device`` list to select the VirtualDisk to edit."
            ),
        },
        "capacity_bytes": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Requested new disk capacity in bytes. Grow-only: a value "
                "less than or equal to the disk's current capacity is refused "
                "with ``status='invalid_shrink'`` before any reconfigure is "
                "issued (vSphere rejects a shrink)."
            ),
        },
    },
    "required": ["vm", "disk", "capacity_bytes"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "disk": "2000", "capacity_bytes": 107374182400}],
}


#: ``vmware.composite.vm.disk.attach`` parameter schema (#3256).
#:
#: Attach an **existing** VMDK to a VM at an explicit SCSI ``controller/unit``
#: address — the shared-attach leg of a WSFC / FCI or multi-writer cluster
#: build (the second node opens the same disk the first node created). Rides
#: vim ``ReconfigVM_Task`` with a ``VirtualDeviceConfigSpec`` ``add`` that
#: carries **no** ``fileOperation`` (``fileOperation="create"`` would make a
#: new VMDK; its absence attaches the existing one). The pinned REST spec's
#: ``Disk.CreateSpec.backing`` can attach an existing VMDK but cannot set the
#: multi-writer ``sharing`` flag, so the governed path is vim-uniform (works
#: on vSphere 8.x and 9.x, like ``vm.disk.grow``). Fail-closed before any
#: write: an ill-formed ``vmdk_path``, an out-of-range ``unit_number``, a
#: missing / non-SCSI ``controller_key``, or an occupied unit each return a
#: structured status and issue no reconfigure.
VM_DISK_ATTACH_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Managed-object ID of the VM to attach the disk to (e.g. 'vm-42').",
        },
        "vmdk_path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Datastore path of the existing VMDK to attach, in the "
                "canonical ``[datastore] dir/disk.vmdk`` form (e.g. "
                "``[vsanDatastore] wsfc-quorum/quorum.vmdk``). Validated "
                "before any write; a value that does not match the "
                "``[datastore] path.vmdk`` shape, or that carries a ``..`` "
                "path segment, is refused with ``status='invalid_vmdk_path'`` "
                "(there is no shell on this path — the check rejects malformed "
                "shape, control chars, and path traversal before the "
                "reconfigure body is built)."
            ),
        },
        "controller_key": {
            "type": "integer",
            "description": (
                "Device key of the target SCSI controller already present on "
                "the VM (the vim ``VirtualController.key``, e.g. 1000). The "
                "handler reads the VM's ``config.hardware.device`` and refuses "
                "with ``status='controller_not_found'`` when no device with "
                "this key exists or it is not a SCSI controller. For WSFC/FCI "
                "this is a dedicated bus-shared controller (create it on the "
                "VM with ``scsi_bus_sharing`` on ``vm.create``); the boot "
                "controller must not be shared."
            ),
        },
        "unit_number": {
            "type": "integer",
            "minimum": 0,
            "maximum": 15,
            "description": (
                "SCSI unit number on ``controller_key`` to attach the disk at "
                "(0-15, excluding 7 which the controller reserves). Both "
                "cluster nodes attach the shared VMDK at the **same** "
                "controller/unit address. An occupied unit is refused with "
                "``status='unit_in_use'``; unit 7 (or out of range) with "
                "``status='invalid_unit'`` — both before any write."
            ),
        },
        "sharing": {
            "type": "string",
            "enum": ["none", "multi_writer"],
            "default": "none",
            "description": (
                "Per-disk multi-writer sharing on the attached backing, "
                "distinct from the controller's bus-sharing. ``multi_writer`` "
                "sets ``sharing=sharingMultiWriter`` (application-managed "
                "clustering, e.g. Oracle RAC). WSFC/FCI does NOT use "
                "multi-writer — it relies on SCSI-3 persistent reservations "
                "from a ``physical`` bus-sharing controller instead — so leave "
                "this ``none`` and attach to a bus-shared ``controller_key`` "
                "for a Windows failover cluster."
            ),
        },
    },
    "required": ["vm", "vmdk_path", "controller_key", "unit_number"],
    "additionalProperties": False,
    "examples": [
        {
            "vm": "vm-42",
            "vmdk_path": "[<datastore-name>] <folder>/<disk-name>.vmdk",
            "controller_key": 1000,
            "unit_number": 1,
            "sharing": "multi_writer",
        }
    ],
}


#: ``vmware.composite.cluster.drs_rule.create`` parameter schema.
#:
#: Add a DRS affinity / anti-affinity rule by *explicit VM list* via vim
#: ``ClusterComputeResource.ReconfigureComputeResource_Task`` (no cluster-rules
#: REST path exists; the tag-based compute-policies surface is semantically
#: wrong — tag-scoped, not an explicit VM list). Rule names are the
#: idempotence key: a duplicate returns ``status='rule_exists'`` before any
#: write. VM names resolve to MoRefs scoped to the cluster; fewer than two
#: resolve → ``status='insufficient_vms'``.
CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": "ClusterComputeResource moid the rule is added to (e.g. 'domain-c1').",
        },
        "rule_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the new rule. Rule names are the idempotence key — a "
                "name already present on the cluster returns "
                "``status='rule_exists'`` before any write, not a raw vim "
                "``DuplicateName`` fault."
            ),
        },
        "rule_type": {
            "type": "string",
            "enum": ["affinity", "anti_affinity"],
            "description": (
                "``'affinity'`` keeps the VMs on the same host "
                "(``ClusterAffinityRuleSpec``); ``'anti_affinity'`` keeps them "
                "on separate hosts (``ClusterAntiAffinityRuleSpec``)."
            ),
        },
        "vms": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 2,
            "description": (
                "Display names of the VMs the rule governs. Resolved to MoRefs "
                "scoped to the cluster (a name not naming a VM in the cluster is "
                "dropped); fewer than two resolve → ``status='insufficient_vms'``."
            ),
        },
        "enabled": {
            "type": "boolean",
            "default": True,
            "description": "Whether the rule is enabled on creation. Defaults to true.",
        },
    },
    "required": ["cluster", "rule_name", "rule_type", "vms"],
    "additionalProperties": False,
    "examples": [
        {
            "cluster": "domain-c7",
            "rule_name": "<rule-name>",
            "rule_type": "anti_affinity",
            "vms": ["<vm-name-1>", "<vm-name-2>"],
        }
    ],
}


#: ``vmware.composite.cluster.drs_vm_host_rule.create`` parameter schema.
#:
#: A VM-Host affinity rule (``ClusterVmHostRuleInfo``) pins the VMs of a named
#: VM group onto (``affine=true``) or away from (``affine=false``) the hosts of
#: a named host group. A **sibling** of ``cluster.drs_rule.create`` (whose
#: contract stays unchanged), because a VM-Host rule needs a host group + VM
#: group, not the VM-VM explicit list. VM + host names resolve to MoRefs scoped
#: to the cluster; rule + group names are the idempotence keys.
CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": "ClusterComputeResource moid the rule is added to (e.g. 'domain-c1').",
        },
        "rule_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the new VM-Host rule. Rule names are an idempotence key — a name "
                "already present returns ``status='rule_exists'`` before any write."
            ),
        },
        "vm_group_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the ClusterVmGroup to create for the rule's VMs. Group names are "
                "unique within a cluster — a clash returns ``status='group_exists'``."
            ),
        },
        "host_group_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the ClusterHostGroup to create for the rule's hosts. Group names are "
                "unique within a cluster — a clash returns ``status='group_exists'``."
            ),
        },
        "vms": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "Display names of the VMs in the VM group. Resolved to MoRefs scoped to the "
                "cluster; none resolving → ``status='insufficient_vms'``."
            ),
        },
        "hosts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "Display names of the hosts in the host group. Resolved to MoRefs scoped to the "
                "cluster; none resolving → ``status='insufficient_hosts'``."
            ),
        },
        "affine": {
            "type": "boolean",
            "default": True,
            "description": (
                "``true`` (default) makes the host group the *affine* group (VMs run on those "
                "hosts); ``false`` makes it the *anti-affine* group (VMs avoid those hosts)."
            ),
        },
        "mandatory": {
            "type": "boolean",
            "default": False,
            "description": (
                "``true`` = a 'must' rule (DRS never violates it, even for HA/maintenance); "
                "``false`` (default) = a 'should' rule (a soft preference DRS may relax)."
            ),
        },
        "enabled": {
            "type": "boolean",
            "default": True,
            "description": "Whether the rule is enabled on creation. Defaults to true.",
        },
    },
    "required": ["cluster", "rule_name", "vm_group_name", "host_group_name", "vms", "hosts"],
    "additionalProperties": False,
    "examples": [
        {
            "cluster": "domain-c7",
            "rule_name": "<rule-name>",
            "vm_group_name": "<vm-group-name>",
            "host_group_name": "<host-group-name>",
            "vms": ["<vm-name>"],
            "hosts": ["<host-name>"],
            "affine": True,
            "mandatory": False,
        }
    ],
}


#: Nested ``cpu_allocation`` / ``memory_allocation`` sub-schema for the
#: resource-pool create (``Vcenter.ResourcePool.ResourceAllocationCreateSpec``).
#: All optional — an omitted allocation uses vCenter's documented default.
_RESOURCE_ALLOCATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reservation": {
            "type": "integer",
            "description": (
                "Guaranteed resource (MHz for CPU, MB for memory). Default 0 when omitted."
            ),
        },
        "expandable_reservation": {
            "type": "boolean",
            "description": (
                "Whether the reservation can grow into the parent's unreserved resources. "
                "Default true when omitted."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                "Utilisation ceiling (MHz for CPU, MB for memory); -1 = unbounded. Default -1."
            ),
        },
        "shares": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["LOW", "NORMAL", "HIGH", "CUSTOM"],
                    "description": "Shares level; CUSTOM uses the numeric ``shares`` count.",
                },
                "shares": {
                    "type": "integer",
                    "description": "Custom shares count; only used when ``level`` is CUSTOM.",
                },
            },
            "required": ["level"],
            "additionalProperties": False,
            "description": "Relative-weight shares under contention. Default NORMAL when omitted.",
        },
    },
    "additionalProperties": False,
}


#: ``vmware.composite.resource_pool.create`` parameter schema.
#:
#: Create a resource pool via the REST ``POST:/vcenter/resource-pool``
#: (``Vcenter.ResourcePool.CreateSpec``). Exactly one parent selector:
#: ``parent`` (a ResourcePool moid, used verbatim) or ``cluster`` (a
#: ClusterComputeResource moid whose *root* resource pool is resolved as the
#: parent — the estate-allocation convenience).
RESOURCE_POOL_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the new resource pool.",
        },
        "parent": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Parent ResourcePool moid the new pool nests under (e.g. 'resgroup-42'). "
                "Mutually exclusive with ``cluster``."
            ),
        },
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "ClusterComputeResource moid (e.g. 'domain-c1') whose root resource pool "
                "becomes the parent — resolved via ClusterComputeResource.resourcePool. "
                "Mutually exclusive with ``parent``."
            ),
        },
        "cpu_allocation": {
            **_RESOURCE_ALLOCATION_SCHEMA,
            "description": (
                "CPU reservation, limit and shares of the new pool, in MHz "
                "(``Vcenter.ResourcePool.ResourceAllocationCreateSpec``). Omitted: vCenter's "
                "default CPU allocation."
            ),
        },
        "memory_allocation": {
            **_RESOURCE_ALLOCATION_SCHEMA,
            "description": (
                "Memory reservation, limit and shares of the new pool, in MB "
                "(``Vcenter.ResourcePool.ResourceAllocationCreateSpec``). Omitted: vCenter's "
                "default memory allocation."
            ),
        },
    },
    "required": ["name"],
    "oneOf": [{"required": ["parent"]}, {"required": ["cluster"]}],
    "additionalProperties": False,
    "examples": [
        {
            "name": "<pool-name>",
            "cluster": "domain-c7",
            "cpu_allocation": {"reservation": 2000, "limit": -1},
            "memory_allocation": {"reservation": 4096, "shares": {"level": "NORMAL"}},
        }
    ],
}


#: ``vmware.composite.resource_pool.delete`` parameter schema.
#:
#: Delete a resource pool via the REST
#: ``DELETE:/vcenter/resource-pool/{resourcePool}``, which reparents the pool's
#: child pools + VMs up to its parent (it does not destroy them). A non-empty
#: pool is refused unless ``force=true``.
RESOURCE_POOL_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resource_pool": {
            "type": "string",
            "minLength": 1,
            "description": "ResourcePool moid to delete (e.g. 'resgroup-42').",
        },
        "force": {
            "type": "boolean",
            "default": False,
            "description": (
                "Delete even when the pool has child pools / VMs (they are reparented up to the "
                "parent). Without it, a non-empty pool returns ``status='not_empty'``."
            ),
        },
    },
    "required": ["resource_pool"],
    "additionalProperties": False,
    "examples": [{"resource_pool": "resgroup-42"}],
}


#: VM folder moids only (``group-v``), e.g. a datacenter's root ``vm`` folder.
VM_FOLDER_MOID_PATTERN: str = r"^group-v[0-9]+$"
#: Datacenter moids, e.g. ``datacenter-3``.
DATACENTER_MOID_PATTERN: str = r"^datacenter-[0-9]+$"


#: ``vmware.composite.folder.create`` parameter schema.
#:
#: Create a VM folder under a parent via the **synchronous** vim
#: ``Folder.CreateFolder`` (``/vcenter/folder`` is GET-only, so vim is the
#: sole write path). The parent is either a display name (``parent_folder``,
#: resolved among the ``VIRTUAL_MACHINE`` folders, optionally scoped to one
#: ``datacenter``) or an explicit folder ID (``parent_folder_id``) that skips
#: the lookup — the ``vm.create`` ``folder_name`` / ``folder`` shape (#3115).
#: Every datacenter ships a root VM folder named ``vm``, so on a
#: multi-datacenter vCenter the bare name ``vm`` needs one of the two (#3588).
#: An unresolved parent fails the operation (``parent_not_found`` /
#: ``ambiguous_parent`` with an ``error`` issue), it is never a silent no-op.
FOLDER_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "parent_folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the parent VM folder the new folder is created "
                "under. Resolved to its moid via ``GET:/vcenter/folder`` filtered "
                "to VIRTUAL_MACHINE folders (and to ``datacenter`` when given). "
                "Every datacenter has a root VM folder named ``vm``, so on a "
                "multi-datacenter vCenter pass ``datacenter`` or "
                "``parent_folder_id`` too. An unknown or ambiguous name fails the "
                "operation before any write. Ignored when ``parent_folder_id`` "
                "is supplied."
            ),
        },
        "parent_folder_id": {
            "type": "string",
            "pattern": VM_FOLDER_MOID_PATTERN,
            "description": (
                "Optional folder ID (moid, ``group-v...``) of the parent VM "
                "folder, like the ``vm.create`` ``folder`` field. When "
                "present the name lookup is skipped and the folder is created "
                "under this ID. One of ``parent_folder`` / ``parent_folder_id`` "
                "is required."
            ),
        },
        "datacenter": {
            "type": "string",
            "pattern": DATACENTER_MOID_PATTERN,
            "description": (
                "Optional datacenter moid (``datacenter-...``) that scopes the "
                "``parent_folder`` name lookup via ``filter.datacenters``. Use "
                "it to pick one datacenter's root ``vm`` folder. Ignored when "
                "``parent_folder_id`` is supplied."
            ),
        },
        "folder_name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the new sub-folder to create under the parent.",
        },
    },
    "required": ["folder_name"],
    "anyOf": [
        {"required": ["parent_folder"]},
        {"required": ["parent_folder_id"]},
    ],
    "additionalProperties": False,
    "examples": [{"parent_folder_id": "group-v42", "folder_name": "<new-folder-name>"}],
}


#: ``vmware.composite.network.portgroup.create`` parameter schema.
#:
#: Create a distributed portgroup on an existing DVS via the **vim**
#: ``DistributedVirtualSwitch.CreateDVPortgroup_Task`` (the pinned vcenter.yaml
#: serves no portgroup-create REST path). ``vds`` is the switch moid (no
#: portgroup/switch REST list exists to resolve a name against — the moid comes
#: from the ``networking`` group's ``portgroup.audit`` read). Exactly one VLAN
#: mode: ``vlan_trunk_ranges`` (trunk — the nested-ESXi substrate case) OR
#: ``vlan_id`` (a single access VLAN), never both: the ``not`` clause makes MEHO
#: refuse both before the call. Neither keeps the switch's default VLAN.
NETWORK_PORTGROUP_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vds": {
            "type": "string",
            "minLength": 1,
            "description": (
                "DistributedVirtualSwitch moid the portgroup is created on (e.g. "
                "'dvs-16'). There is no portgroup/switch REST list to resolve a "
                "display name against, so the moid is passed directly — obtain it "
                "from the networking group's portgroup.audit read."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the new distributed portgroup.",
        },
        "vlan_trunk_ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4094,
                        "description": "First VLAN id of the range.",
                    },
                    "end": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4094,
                        "description": "Last VLAN id of the range (inclusive).",
                    },
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "description": (
                "Trunk-mode VLAN ranges (VmwareDistributedVirtualSwitchTrunkVlanSpec "
                "— a NumericRange[] of inclusive start..end pairs, each 0..4094). "
                "Use for a nested-ESXi trunk portgroup (e.g. [{start:0,end:4094}] at "
                "bootstrap). Mutually exclusive with vlan_id."
            ),
        },
        "vlan_id": {
            "type": "integer",
            "minimum": 0,
            "maximum": 4094,
            "description": (
                "Single access VLAN id (VmwareDistributedVirtualSwitchVlanIdSpec); "
                "0 means no VLAN. Mutually exclusive with vlan_trunk_ranges. Omit "
                "both to inherit the switch default."
            ),
        },
        "num_ports": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Number of ports in the portgroup (DVPortgroupConfigSpec.numPorts). "
                "Omit to let the switch pick the default. Ignored for ephemeral "
                "port_binding."
            ),
        },
        "port_binding": {
            "type": "string",
            "enum": ["earlyBinding", "lateBinding", "ephemeral"],
            "default": "earlyBinding",
            "description": (
                "Portgroup binding type (DistributedVirtualPortgroupPortgroupType). "
                "Defaults to 'earlyBinding' (static) — the right choice for a "
                "nested-ESXi trunk portgroup."
            ),
        },
    },
    "required": ["vds", "name"],
    "not": {"required": ["vlan_trunk_ranges", "vlan_id"]},
    "additionalProperties": False,
    "examples": [
        {
            "vds": "dvs-31",
            "name": "<portgroup-name>",
            "vlan_trunk_ranges": [{"start": 0, "end": 4094}],
        }
    ],
}


#: ``vmware.composite.network.portgroup.security.set`` parameter schema.
#:
#: Set any of the security-policy triple (allowPromiscuous / forgedTransmits /
#: macChanges) on a distributed portgroup via the **vim**
#: ``DistributedVirtualPortgroup.ReconfigureDVPortgroup_Task`` (the pinned
#: vcenter.yaml serves no security-policy write). Only the booleans supplied
#: are changed; none supplied → ``status='no_change_requested'`` before any
#: write. The required ``configVersion`` and the current policy are read first.
NETWORK_PORTGROUP_SECURITY_SET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "portgroup": {
            "type": "string",
            "minLength": 1,
            "description": (
                "DistributedVirtualPortgroup moid whose security policy is set "
                "(e.g. 'dvportgroup-42'). Obtain it from the networking group's "
                "portgroup.audit read."
            ),
        },
        "allow_promiscuous": {
            "type": "boolean",
            "description": (
                "Set DVSSecurityPolicy.allowPromiscuous. Accept (true) is required "
                "for a nested-ESXi trunk portgroup so the nested switch sees frames "
                "for MACs it owns. Governance-sensitive — a promiscuous portgroup "
                "sees all traffic on its VLANs."
            ),
        },
        "forged_transmits": {
            "type": "boolean",
            "description": (
                "Set DVSSecurityPolicy.forgedTransmits. Accept (true) lets a nested "
                "ESXi emit frames with a source MAC other than the vNIC's."
            ),
        },
        "mac_changes": {
            "type": "boolean",
            "description": (
                "Set DVSSecurityPolicy.macChanges. Accept (true) lets the guest "
                "change the effective MAC (nested ESXi needs this for its vmks)."
            ),
        },
    },
    "required": ["portgroup"],
    "additionalProperties": False,
    "examples": [
        {
            "portgroup": "dvportgroup-42",
            "allow_promiscuous": True,
            "forged_transmits": True,
            "mac_changes": True,
        }
    ],
}


#: ``vmware.composite.network.portgroup.vlan.set`` parameter schema.
#:
#: Reconfigure an EXISTING distributed portgroup's VLAN config via the **vim**
#: ``DistributedVirtualPortgroup.ReconfigureDVPortgroup_Task`` (the pinned
#: vcenter.yaml serves no VLAN write). ``portgroup`` is the moid (obtain it
#: from the networking group's ``portgroup.audit`` read), and exactly one VLAN
#: mode -- ``vlan_trunk_ranges`` (a NumericRange[] trunk) XOR ``vlan_id`` (a
#: single access VLAN); the ``oneOf`` makes MEHO refuse both or neither before
#: the call. The spec **REPLACES** the current VLAN config; there is
#: no merge, so to add the untagged/native VLAN 0 to an existing trunk band the
#: operator passes the full desired range list (e.g.
#: ``[{start:0,end:0},{start:3251,end:3271}]``). Idempotent -- a request that
#: already equals the current config returns ``status='unchanged'`` without a
#: write.
NETWORK_PORTGROUP_VLAN_SET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "portgroup": {
            "type": "string",
            "minLength": 1,
            "description": (
                "DistributedVirtualPortgroup moid whose VLAN config is set (e.g. "
                "'dvportgroup-42'). Obtain it from the networking group's "
                "portgroup.audit read."
            ),
        },
        "vlan_trunk_ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4094,
                        "description": "First VLAN id of the range.",
                    },
                    "end": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4094,
                        "description": "Last VLAN id of the range (inclusive).",
                    },
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "description": (
                "Trunk-mode VLAN ranges (VmwareDistributedVirtualSwitchTrunkVlanSpec "
                "-- a NumericRange[] of inclusive start..end pairs, each 0..4094). This "
                "list REPLACES the current VLAN config (no merge): to add the untagged "
                "VLAN 0 to an existing trunk, pass the full desired list including "
                "{start:0,end:0}. Mutually exclusive with vlan_id."
            ),
        },
        "vlan_id": {
            "type": "integer",
            "minimum": 0,
            "maximum": 4094,
            "description": (
                "Single access VLAN id (VmwareDistributedVirtualSwitchVlanIdSpec); "
                "0 means no/native VLAN. Replaces the current VLAN config. Mutually "
                "exclusive with vlan_trunk_ranges."
            ),
        },
        "replace": {
            "type": "boolean",
            "default": True,
            "description": (
                "The given spec REPLACES the current VLAN config -- there is no merge. "
                "Defaults to true (the only supported mode); replace=false refuses with "
                "status='invalid_vlan_spec' because a merge is not implemented (pass the "
                "full desired range list instead)."
            ),
        },
    },
    "required": ["portgroup"],
    "oneOf": [{"required": ["vlan_trunk_ranges"]}, {"required": ["vlan_id"]}],
    "additionalProperties": False,
    "examples": [
        {
            "portgroup": "dvportgroup-42",
            "vlan_trunk_ranges": [{"start": 0, "end": 0}, {"start": 100, "end": 200}],
        }
    ],
}


# ---------------------------------------------------------------------------
# Write composites -- response schemas
# ---------------------------------------------------------------------------
#
# Each composite's response shape encodes the documented status enum
# (``"created"`` / ``"rolled_back"`` / ``"timeout"`` / ``"ambiguous"``
# / ``"no_recommendation"`` / ``"ok"`` / ``"incomplete"`` /
# ``"stopped"``) so callers can branch on ``status`` without parsing
# free-form prose. Sub-payload shapes (vSphere REST payloads, task
# ids) stay opaque -- Broadcom owns the inner schema.


#: ``vmware.composite.vm.create`` response schema.
VM_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["created", "rolled_back"],
            "description": (
                "``'created'`` after every step succeeded; "
                "``'rolled_back'`` when a step failed. If a VM was "
                "already created, the handler tried "
                "``DELETE:/vcenter/vm/{vm}`` and ``rollback.outcome`` "
                "says what happened."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": (
                "Newly-created VM moid. ``null`` when "
                "``status='rolled_back'``; read ``rollback`` to see "
                "whether a VM this run created still exists."
            ),
        },
        "steps_succeeded": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Per-step success ledger: ``folder_lookup``, "
                "``create``, ``disk_attach``, ``nic_attach``, "
                "``nested_hv``, ``power_on``. ``disk_attach`` is "
                "present when the request carried ``disks`` (folded into "
                "the create on both arms). ``folder_lookup`` is omitted "
                "when the request pinned the folder moid via the "
                "``folder`` param (no lookup ran)."
            ),
        },
        "failed_step": {
            "type": ["string", "null"],
            "description": (
                "Name of the first failing step on rollback; ``null`` "
                "when ``status='created'``. Both arms fail closed on an "
                "invalid ``disks`` entry with ``disk_spec``. The "
                "pre-9.0 vim arm adds fail-closed resolution "
                "steps: ``guest_id_mapping``, ``placement_params``, "
                "``datastore_lookup``, ``network_lookup``."
            ),
        },
        "rollback_reason": {
            "type": ["string", "null"],
            "description": (
                "Human-readable explanation of the rollback trigger; "
                "``null`` when ``status='created'``."
            ),
        },
        "rollback": {
            "type": "object",
            "properties": {
                "vm_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["removed", "awaiting_approval", "not_removed"],
                },
            },
            "description": (
                "What happened to the VM this run created. Present only "
                "when a step after the create failed. ``removed``: the rollback "
                "deleted the VM. ``awaiting_approval``: the delete waits for "
                "approval and the VM exists until then. ``not_removed``: the "
                "delete failed or was not allowed; delete ``vm_id`` by hand. The "
                "error issue says the same in words."
            ),
        },
        "nested_hv": {
            "type": "boolean",
            "description": (
                "Applied VHV state. Present only when the request "
                "carried the ``nested_hv`` param. ``true`` "
                "iff the ``ReconfigVM_Task`` leg completed."
            ),
        },
        "candidate_folders": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Folder moids an ambiguous ``folder_name`` matched. "
                "Present only on a ``folder_lookup`` rollback "
                "when the name matched more than one folder (or matched "
                "only outside the placement pins' datacenter); re-issue "
                "with the intended moid as the ``folder`` param."
            ),
        },
    },
    "required": ["status", "steps_succeeded"],
}


#: ``vmware.composite.vm.clone`` response schema.
VM_CLONE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed"],
            "description": (
                "``'completed'`` -- the synchronous deploy returned the "
                "new VM id. Deploy failures raise and surface as "
                "``connector_error`` rather than a status."
            ),
        },
        "task_id": {
            "type": ["string", "null"],
            "description": (
                "Always ``null``: the pinned deploy operation is "
                "synchronous (no cis task). Key retained for "
                "response-envelope stability."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": "Deployed VM moid (the deploy operation's 200 body).",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "``null`` on ``status='completed'``.",
        },
    },
    "required": ["status", "task_id"],
}


#: ``vmware.composite.vm.clone_from_template`` response schema.
VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "cloned",
                "template_not_found",
                "ambiguous_template",
                "not_a_template",
                "timeout",
            ],
            "description": (
                "``'cloned'`` — the CloneVM_Task reached terminal success; "
                "``'template_not_found'`` — the source name matched no VM; "
                "``'ambiguous_template'`` — the name matched more than one VM; "
                "``'not_a_template'`` — the source resolved but is not a "
                "marked-as-template VM (``config.template`` is not true); "
                "``'timeout'`` — the clone task did not reach a terminal state "
                "within the poll bound (it may still complete in the "
                "background). A vim task *fault* raises (wrapped "
                "``connector_error``), it is not a status here."
            ),
        },
        "source_template": {
            "type": "string",
            "description": "The source template display name that was requested.",
        },
        "source_template_id": {
            "type": ["string", "null"],
            "description": (
                "The resolved source VM moid; ``null`` on ``template_not_found`` "
                "/ ``ambiguous_template`` (nothing uniquely resolved)."
            ),
        },
        "new_vm_name": {
            "type": "string",
            "description": "The requested display name for the clone.",
        },
        "new_vm_id": {
            "type": ["string", "null"],
            "description": (
                "The cloned VM's moid, read from the CloneVM_Task result on "
                "success; ``null`` on every non-``cloned`` status."
            ),
        },
        "folder": {
            "type": "string",
            "description": "The destination folder moid the clone was placed in.",
        },
        "task": {
            "type": ["string", "null"],
            "description": (
                "CloneVM_Task moid — present once the clone write was issued "
                "(``cloned`` / ``timeout``); ``null`` on the pre-write "
                "refusals (``template_not_found`` / ``ambiguous_template`` / "
                "``not_a_template``)."
            ),
        },
        "customization_spec_name": {
            "type": ["string", "null"],
            "description": (
                "Echo of the applied GOSC spec name, or ``null`` when the "
                "clone requested no inline customization."
            ),
        },
        "candidates": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "On ``ambiguous_template``, the moids the name matched so the "
                "operator can re-issue against an unambiguous template; "
                "``null`` otherwise."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on non-``cloned`` statuses; "
                "``null`` when ``status='cloned'``."
            ),
        },
    },
    "required": ["status", "source_template", "new_vm_name"],
}


#: ``vmware.composite.vm.snapshot.revert`` response schema.
VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["reverted", "ambiguous", "not_found", "timeout"],
            "description": (
                "``'reverted'`` on a successful revert; "
                "``'ambiguous'`` when multiple snapshots share the "
                "name; ``'not_found'`` when no snapshot matches; "
                "``'timeout'`` when the RevertToSnapshot_Task poll "
                "deadline elapsed (the revert may still complete in "
                "the background)."
            ),
        },
        "snapshot_id": {
            "type": ["string", "null"],
            "description": (
                "The moid of the snapshot the handler reverted to; ``null`` on ambiguous/not_found."
            ),
        },
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Snapshot name."},
                    "snapshot": {"type": "string", "description": "Snapshot moid."},
                },
            },
            "description": (
                "Ambiguity-resolution candidates -- present only when ``status='ambiguous'``."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": ("Operator hint on ambiguous/not_found; ``null`` on successful revert."),
        },
    },
    "required": ["status"],
}


#: ``vmware.composite.vm.destroy`` response schema (#3198).
VM_DESTROY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["destroyed", "not_powered_off", "not_found", "timeout"],
            "description": (
                "``'destroyed'`` on a successful delete; ``'not_powered_off'`` "
                "when the VM is not POWERED_OFF (refused fail-closed, no "
                "implicit power-off); ``'not_found'`` when the VM cannot be "
                "read; ``'timeout'`` when the vim Destroy_Task poll deadline "
                "elapsed (the destroy may still complete in the background)."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": "The destroyed VM's moid; ``null`` on not_found.",
        },
        "power_state": {
            "type": ["string", "null"],
            "description": "The VM's power state at dispatch time (the fail-closed gate reads it).",
        },
        "arm": {
            "type": ["string", "null"],
            "enum": ["vim", "rest", None],
            "description": (
                "Which delete arm ran: ``'vim'`` (pre-9.0 Destroy_Task) or "
                "``'rest'`` (9.0+ DELETE:/vcenter/vm/{vm}); ``null`` when refused."
            ),
        },
        "task_id": {
            "type": ["string", "null"],
            "description": "The vim Destroy_Task moid on the vim arm; ``null`` on the REST arm.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Operator hint on refusal/timeout; ``null`` on a successful destroy.",
        },
    },
    "required": ["status"],
}


#: ``vmware.composite.vm.migrate`` response schema.
VM_MIGRATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["migrated", "no_recommendation"],
            "description": (
                "``'migrated'`` after a successful relocate; "
                "``'no_recommendation'`` when DRS returned nothing and "
                "no ``target_host`` override was supplied."
            ),
        },
        "target_host": {
            "type": ["string", "null"],
            "description": ("Host moid the relocate targeted; ``null`` on ``no_recommendation``."),
        },
        "source": {
            "type": "string",
            "enum": ["drs", "operator", "none"],
            "description": (
                "Whether the target came from a DRS recommendation, "
                "the operator's explicit override, or neither."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": ("Operator hint on ``no_recommendation``; ``null`` otherwise."),
        },
    },
    "required": ["status", "source"],
}


#: ``vmware.composite.vm.power.bulk`` response schema.
VM_POWER_BULK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string"},
                    "status": {"type": "string", "enum": ["ok", "error"]},
                    "error": {"type": ["string", "null"]},
                },
                "required": ["vm", "status"],
            },
            "description": "One row per VM the filter matched.",
        },
        "summary": {
            "type": "object",
            "properties": {
                "ok": {"type": "integer", "minimum": 0},
                "error": {"type": "integer", "minimum": 0},
            },
            "required": ["ok", "error"],
            "description": "Aggregate counts across ``results``.",
        },
        "aborted_on_failure": {
            "type": "boolean",
            "description": (
                "True when ``fail_fast=True`` short-circuited the loop after the first failure."
            ),
        },
    },
    "required": ["results", "summary", "aborted_on_failure"],
}


#: ``vmware.composite.vm.power`` response schema.
VM_POWER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "vm": {"type": "string", "description": "VM moid the verb targeted."},
        "verb": {
            "type": "string",
            "enum": ["on", "off", "reset", "guest_shutdown", "guest_reboot"],
            "description": "Verb applied.",
        },
        "status": {
            "type": "string",
            "enum": ["ok", "error", "tools_unavailable"],
            "description": (
                "``'ok'`` -- the power verb issued; ``'tools_unavailable'`` "
                "-- a soft verb could not run because VMware Tools is not "
                "running (typed failure, not a hang); ``'error'`` -- any "
                "other transport / vCenter fault."
            ),
        },
        "error": {
            "type": ["string", "null"],
            "description": "Fault text when ``status != 'ok'``; ``null`` on success.",
        },
        "error_type": {
            "type": ["string", "null"],
            "description": (
                "vCenter machine ``error_type`` parsed from the fault body "
                "when present (e.g. ``SERVICE_UNAVAILABLE``); ``null`` when "
                "unparseable or on success."
            ),
        },
        "guest_tools": {
            "type": ["string", "null"],
            "enum": ["ok", "unavailable", None],
            "description": (
                "Tools state for a soft verb: ``'ok'`` when the guest "
                "request issued, ``'unavailable'`` when Tools is down. "
                "``null`` for the hard verbs, which do not consult Tools."
            ),
        },
    },
    "required": ["vm", "verb", "status"],
}


#: ``vmware.composite.host.evacuate`` response schema.
HOST_EVACUATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["evacuated", "partial", "aborted", "timeout", "not_in_cluster"],
            "description": (
                "``evacuated``: the host is in maintenance mode and no "
                "running VM stayed on it (powered-off VMs may stay). "
                "``partial``: the host is in maintenance mode but some "
                "running VMs stayed (``tolerate_partial_failure``). "
                "``aborted``: a VM could not be moved, so the host did not "
                "enter maintenance mode. ``timeout``: MEHO stopped waiting "
                "while the maintenance-mode task still runs; the call did "
                "not fail. ``not_in_cluster``: the host is not in a cluster; "
                "nothing was changed."
            ),
        },
        "host": {
            "type": "string",
            "description": "Host moid the operator targeted.",
        },
        "migrated_vms": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Moids of the VMs that left the host, moved by MEHO or by "
                "vCenter's DRS (see ``moved_by``)."
            ),
        },
        "failed_vms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["vm", "error"],
            },
            "description": (
                "VMs MEHO tried to move and could not, with the reason "
                "(for example ``no_recommendation``). A VM that left the "
                "host anyway is not listed here."
            ),
        },
        "remaining_vms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "power_state": {"type": ["string", "null"]},
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why the VM stayed: ``powered_off`` / ``suspended`` "
                            "(only running VMs are moved), the move error, "
                            "``still_listed_after_move`` (vm.migrate reported the "
                            "move as done, but the host still lists the VM), "
                            "``not_moved`` (vCenter left it on the host) or "
                            "``not_attempted`` (the operation stopped before "
                            "reaching it)."
                        ),
                    },
                },
                "required": ["vm", "reason"],
            },
            "description": "VMs still on the host after the operation, each with its reason.",
        },
        "maintenance_entered": {
            "type": "boolean",
            "description": (
                "Whether the host is in maintenance mode: true on "
                "``evacuated`` / ``partial``, false otherwise."
            ),
        },
        "maintenance_task": {
            "type": ["string", "null"],
            "description": (
                "The vCenter task id of the maintenance-mode request; on "
                "``timeout`` the task still runs under this id."
            ),
        },
        "moved_by": {
            "type": "string",
            "enum": ["drs", "meho", "none"],
            "description": (
                "``drs``: DRS is fully automated, so vCenter moved the VMs as "
                "part of the maintenance-mode task. ``meho``: MEHO moved each "
                "running VM where DRS recommended (vm.migrate). ``none``: no "
                "VM was moved (``not_in_cluster``)."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next, on ``timeout`` and ``not_in_cluster``.",
        },
    },
    "required": [
        "status",
        "host",
        "migrated_vms",
        "failed_vms",
        "remaining_vms",
        "maintenance_entered",
        "moved_by",
    ],
}

HOST_MAINTENANCE_EXIT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["exited", "unchanged", "not_connected", "timeout"],
            "description": (
                "``exited``: the host left maintenance mode. ``unchanged``: "
                "the host was not in maintenance mode; nothing was sent. "
                "``not_connected``: the host is not connected to vCenter; "
                "nothing was sent. ``timeout``: MEHO stopped waiting while "
                "the exit task still runs; the call did not fail."
            ),
        },
        "host": {
            "type": "string",
            "description": "Host moid the operator targeted.",
        },
        "name": {
            "type": ["string", "null"],
            "description": "The host's display name, as vCenter reports it.",
        },
        "connection_state": {
            "type": ["string", "null"],
            "description": (
                "The host's connection state after the call (``connected`` / "
                "``notResponding`` / ``disconnected``); on ``unchanged`` and "
                "``not_connected`` the state read before the call."
            ),
        },
        "in_maintenance_mode": {
            "type": ["boolean", "null"],
            "description": (
                "Whether the host is in maintenance mode after the call "
                "(false on ``exited``); on ``timeout`` the state while the "
                "task still runs."
            ),
        },
        "ha_state": {
            "type": ["string", "null"],
            "description": (
                "The vSphere HA state of the host (``master``, "
                "``connectedToMaster``, ``election``, ...). Null on a host "
                "outside an HA cluster."
            ),
        },
        "maintenance_task": {
            "type": ["string", "null"],
            "description": (
                "The vCenter task id of the exit request; on ``timeout`` the "
                "task still runs under this id. Null when nothing was sent."
            ),
        },
        "guidance": {
            "type": "string",
            "description": "What to do next, on every status except ``exited``.",
        },
    },
    "required": ["status", "host"],
}


#: ``vmware.composite.host.detach_from_vds`` response schema.
HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["detached", "incomplete", "timeout"],
            "description": (
                "``'detached'`` -- every NIC migrated and the host "
                "removed from the DVS; ``'incomplete'`` -- one or more "
                "NIC migrations failed, the DVS detach was skipped; "
                "``'timeout'`` -- the ReconfigureDvs_Task poll deadline "
                "elapsed (the detach may still complete in the "
                "background)."
            ),
        },
        "host": {
            "type": "string",
            "description": "Host moid the operator targeted.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": ("Operator hint on ``timeout``; absent/``null`` otherwise."),
        },
        "vm_migration_failures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["vm", "error"],
            },
            "description": "Failed NIC migrations (empty on ``detached``).",
        },
        "vms_migrated": {
            "type": "array",
            "items": {"type": "string"},
            "description": "VM moids whose NICs migrated successfully.",
        },
    },
    "required": [
        "status",
        "host",
        "vm_migration_failures",
        "vms_migrated",
    ],
}


#: ``vmware.composite.cluster.patch`` response schema.
CLUSTER_PATCH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "stopped"],
            "description": (
                "``'completed'`` -- every host patched + maintenance "
                "exit succeeded; ``'stopped'`` -- a per-host failure "
                "halted the loop; cluster is left in mixed state."
            ),
        },
        "cluster": {
            "type": "string",
            "description": "Cluster moid the operator targeted.",
        },
        "patched_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": ("Host moids whose maintenance -> patch -> exit succeeded in order."),
        },
        "failed_host": {
            "type": ["string", "null"],
            "description": (
                "Host moid that failed when ``status='stopped'``; ``null`` on ``completed``."
            ),
        },
        "remaining_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": ("Hosts the loop did not get to (empty on ``completed``)."),
        },
        "failure_reason": {
            "type": ["string", "null"],
            "description": ("Human-readable cause of the stop; ``null`` on ``completed``."),
        },
    },
    "required": [
        "status",
        "cluster",
        "patched_hosts",
        "remaining_hosts",
    ],
}


#: ``vmware.composite.vm.disk.grow`` response schema.
VM_DISK_GROW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["grown", "invalid_shrink", "disk_not_found", "timeout"],
            "description": (
                "``'grown'`` — the ReconfigVM_Task edit reached terminal "
                "success; ``'invalid_shrink'`` — the requested capacity was "
                "``<=`` the current size (refused before any write); "
                "``'disk_not_found'`` — no VirtualDisk with the given key (or "
                "no readable capacity) on the VM; ``'timeout'`` — the "
                "reconfigure task did not reach a terminal state within the "
                "poll bound (it may still complete in the background)."
            ),
        },
        "vm": {"type": "string", "description": "VM moid the disk belongs to."},
        "disk": {"type": "string", "description": "Disk id (== vim device key) the grow targeted."},
        "task": {
            "type": ["string", "null"],
            "description": (
                "ReconfigVM_Task moid — present once the write was issued "
                "(``grown`` / ``timeout``); ``null`` on the pre-write refusals."
            ),
        },
        "from_capacity_bytes": {
            "type": ["integer", "null"],
            "description": (
                "The disk's capacity in bytes before the grow; ``null`` on ``disk_not_found``."
            ),
        },
        "to_capacity_bytes": {
            "type": "integer",
            "description": "The requested capacity in bytes.",
        },
        "delta_bytes": {
            "type": ["integer", "null"],
            "description": (
                "``to_capacity_bytes - from_capacity_bytes`` (positive on a "
                "grow, ``<= 0`` on ``invalid_shrink``); ``null`` on ``disk_not_found``."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``grown`` status; ``null`` on a grow."
            ),
        },
    },
    "required": ["status", "vm", "disk", "to_capacity_bytes"],
}


#: ``vmware.composite.vm.disk.attach`` response schema (#3256).
VM_DISK_ATTACH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "attached",
                "invalid_vmdk_path",
                "invalid_unit",
                "controller_not_found",
                "unit_in_use",
                "timeout",
            ],
            "description": (
                "``'attached'`` — the ReconfigVM_Task add reached terminal "
                "success; ``'invalid_vmdk_path'`` — ``vmdk_path`` did not match "
                "the ``[datastore] path.vmdk`` shape or carried a ``..`` path "
                "segment (refused before any "
                "write); ``'invalid_unit'`` — ``unit_number`` was out of range "
                "or the reserved unit 7; ``'controller_not_found'`` — no SCSI "
                "controller with ``controller_key`` on the VM; "
                "``'unit_in_use'`` — a device already occupies that "
                "controller/unit; ``'timeout'`` — the reconfigure task did not "
                "reach a terminal state within the poll bound (it may still "
                "complete in the background)."
            ),
        },
        "vm": {"type": "string", "description": "VM moid the disk was attached to."},
        "vmdk_path": {"type": "string", "description": "Datastore path of the attached VMDK."},
        "controller_key": {
            "type": "integer",
            "description": "Device key of the SCSI controller the disk attached to.",
        },
        "unit_number": {
            "type": "integer",
            "description": "SCSI unit number the disk attached at.",
        },
        "sharing": {
            "type": "string",
            "enum": ["none", "multi_writer"],
            "description": "The multi-writer sharing mode applied to the attached backing.",
        },
        "task": {
            "type": ["string", "null"],
            "description": (
                "ReconfigVM_Task moid — present once the write was issued "
                "(``attached`` / ``timeout``); ``null`` on the pre-write refusals."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``attached`` status; "
                "``null`` on a successful attach."
            ),
        },
    },
    "required": ["status", "vm", "vmdk_path", "controller_key", "unit_number"],
}


#: ``vmware.composite.cluster.drs_rule.create`` response schema.
CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["created", "rule_exists", "insufficient_vms", "timeout"],
            "description": (
                "``'created'`` — the ReconfigureComputeResource_Task add reached "
                "terminal success; ``'rule_exists'`` — a rule with this name is "
                "already present (idempotence key; refused before any write); "
                "``'insufficient_vms'`` — fewer than two of the requested VM names "
                "resolved to a VM in the cluster (refused before any write); "
                "``'timeout'`` — the reconfigure task did not reach a terminal "
                "state within the poll bound (it may still complete in the "
                "background)."
            ),
        },
        "cluster": {"type": "string", "description": "ClusterComputeResource moid."},
        "rule_name": {"type": "string", "description": "Name of the rule."},
        "rule_type": {
            "type": "string",
            "enum": ["affinity", "anti_affinity"],
            "description": "Rule kind requested.",
        },
        "enabled": {"type": "boolean", "description": "Whether the rule was created enabled."},
        "task": {
            "type": ["string", "null"],
            "description": (
                "ReconfigureComputeResource_Task moid — present once the write was "
                "issued (``created`` / ``timeout``); ``null`` on the pre-write refusals."
            ),
        },
        "resolved_vms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                },
            },
            "description": "The ``[{vm, name}]`` MoRefs the rule references, resolved from names.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``created`` status; ``null`` on success."
            ),
        },
    },
    "required": ["status", "cluster", "rule_name", "rule_type"],
}


#: ``vmware.composite.cluster.drs_vm_host_rule.create`` response schema.
CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "created",
                "rule_exists",
                "group_exists",
                "insufficient_vms",
                "insufficient_hosts",
                "timeout",
            ],
            "description": (
                "``'created'`` — the ReconfigureComputeResource_Task add reached terminal "
                "success; ``'rule_exists'`` / ``'group_exists'`` — the rule or a group name is "
                "already present (idempotence keys; refused before any write); "
                "``'insufficient_vms'`` / ``'insufficient_hosts'`` — none of the requested VM / "
                "host names resolved in the cluster (refused before any write); ``'timeout'`` — "
                "the reconfigure task did not reach a terminal state within the poll bound."
            ),
        },
        "cluster": {"type": "string", "description": "ClusterComputeResource moid."},
        "rule_name": {"type": "string", "description": "Name of the rule."},
        "vm_group_name": {"type": "string", "description": "Name of the created VM group."},
        "host_group_name": {"type": "string", "description": "Name of the created host group."},
        "affine": {
            "type": "boolean",
            "description": (
                "Whether the host group is the affine (true) or anti-affine (false) group."
            ),
        },
        "mandatory": {
            "type": "boolean",
            "description": "Whether the rule is a 'must' (true) or 'should' (false) rule.",
        },
        "enabled": {"type": "boolean", "description": "Whether the rule was created enabled."},
        "task": {
            "type": ["string", "null"],
            "description": (
                "ReconfigureComputeResource_Task moid — present once the write was issued "
                "(``created`` / ``timeout``); ``null`` on the pre-write refusals."
            ),
        },
        "resolved_vms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                },
            },
            "description": (
                "The ``[{vm, name}]`` MoRefs placed in the VM group, resolved from names."
            ),
        },
        "resolved_hosts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                },
            },
            "description": (
                "The ``[{host, name}]`` MoRefs placed in the host group, resolved from names."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``created`` status; ``null`` on success."
            ),
        },
    },
    "required": ["status", "cluster", "rule_name", "vm_group_name", "host_group_name"],
}


#: ``vmware.composite.resource_pool.create`` response schema.
RESOURCE_POOL_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["created", "cluster_root_pool_unresolved", "no_parent"],
            "description": (
                "``'created'`` — the POST returned the new pool moid; "
                "``'cluster_root_pool_unresolved'`` — the ``cluster`` convenience could not read "
                "the cluster's root resource pool (pass an explicit ``parent``); ``'no_parent'`` "
                "— neither ``parent`` nor ``cluster`` was supplied."
            ),
        },
        "name": {"type": "string", "description": "The requested pool name."},
        "parent": {
            "type": ["string", "null"],
            "description": "Resolved parent ResourcePool moid; ``null`` on a resolution refusal.",
        },
        "parent_source": {
            "type": ["string", "null"],
            "enum": ["resource_pool", "cluster", None],
            "description": (
                "``resource_pool`` when the parent came from ``parent`` (used as given), "
                "``cluster`` when it is the cluster's root pool; ``null`` when neither was given."
            ),
        },
        "cluster": {
            "type": ["string", "null"],
            "description": (
                "The cluster moid, when the parent was resolved from a cluster; else ``null``."
            ),
        },
        "resource_pool": {
            "type": ["string", "null"],
            "description": "The new pool's moid on success; ``null`` on a refusal.",
        },
        "verified_under_parent": {
            "type": "boolean",
            "description": (
                "Read-back result: whether the new pool lists under the resolved parent "
                "(``filter.parent_resource_pools``)."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``created`` status; ``null`` on success."
            ),
        },
    },
    "required": ["status", "name"],
}


#: ``vmware.composite.resource_pool.delete`` response schema.
RESOURCE_POOL_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["deleted", "not_empty", "delete_unverified"],
            "description": (
                "``'deleted'`` — the DELETE returned and the read-back confirmed the pool is "
                "absent; ``'not_empty'`` — the pool has child pools / VMs and ``force`` was not "
                "set (no write); ``'delete_unverified'`` — the DELETE returned but the pool "
                "still lists on read-back."
            ),
        },
        "resource_pool": {"type": "string", "description": "The ResourcePool moid targeted."},
        "forced": {
            "type": "boolean",
            "description": "Whether ``force`` was set (i.e. a non-empty pool was deleted).",
        },
        "child_pool_count": {
            "type": "integer",
            "description": "Number of child resource pools counted before the delete.",
        },
        "child_vm_count": {
            "type": "integer",
            "description": "Number of child VMs counted before the delete.",
        },
        "verified_absent": {
            "type": "boolean",
            "description": "Read-back result: whether the pool no longer lists.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``deleted`` status; ``null`` on success."
            ),
        },
    },
    "required": ["status", "resource_pool"],
}


#: ``vmware.composite.folder.create`` response schema.
FOLDER_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["created", "parent_not_found", "ambiguous_parent"],
            "description": (
                "``'created'`` — the synchronous CreateFolder returned the new "
                "folder MoRef; ``'parent_not_found'`` — the parent name matched no "
                "VM folder; ``'ambiguous_parent'`` — it matched more than one. "
                "Both refusals happen before any write and fail the operation "
                "(an ``error`` issue, so the audit row reads ``error``)."
            ),
        },
        "parent_folder": {
            "type": ["string", "null"],
            "description": (
                "The parent folder display name the operator supplied; ``null`` "
                "when only the ``parent_folder_id`` pin was given."
            ),
        },
        "parent_folder_id": {
            "type": ["string", "null"],
            "description": "Resolved parent folder moid; ``null`` on a resolution refusal.",
        },
        "datacenter": {
            "type": ["string", "null"],
            "description": "The datacenter moid that scoped the name lookup, if any.",
        },
        "new_folder_name": {"type": "string", "description": "The requested new folder name."},
        "folder": {
            "type": ["string", "null"],
            "description": (
                "The new folder's moid — CreateFolder is synchronous and returns "
                "the MoRef directly (no task poll). ``null`` on a resolution refusal."
            ),
        },
        "candidate_folders": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "On ``ambiguous_parent``: the VM folder moids the name matched. "
                "Re-issue with one of them as ``parent_folder_id``, or scope the "
                "name with ``datacenter``."
            ),
        },
        "issues": {
            "type": "array",
            "items": _ISSUE_ITEM_SCHEMA,
            "description": (
                "Structured problems. A resolution refusal carries one "
                "``error``-severity entry; empty on ``created``."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on a non-``created`` status; ``null`` on success."
            ),
        },
    },
    "required": ["status", "parent_folder", "new_folder_name"],
}


#: A security triple of ``network.portgroup.security.set``; ``null`` = not set.
_PORTGROUP_SECURITY_FIELDS: dict[str, Any] = {
    "allow_promiscuous": {
        "type": ["boolean", "null"],
        "description": "Promiscuous mode (``DVSSecurityPolicy.allowPromiscuous``).",
    },
    "forged_transmits": {
        "type": ["boolean", "null"],
        "description": "Forged transmits (``DVSSecurityPolicy.forgedTransmits``).",
    },
    "mac_changes": {
        "type": ["boolean", "null"],
        "description": "MAC address changes (``DVSSecurityPolicy.macChanges``).",
    },
}

#: The normalised VLAN view of ``network.portgroup.vlan.set``.
_PORTGROUP_VLAN_VIEW_FIELDS: dict[str, Any] = {
    "mode": {
        "type": "string",
        "enum": ["trunk", "access"],
        "description": "A trunk of VLAN ranges, or one access VLAN.",
    },
    "ranges": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "First VLAN id."},
                "end": {"type": "integer", "description": "Last VLAN id (inclusive)."},
            },
        },
        "description": "Trunk ranges (mode 'trunk'), sorted, touching ranges merged.",
    },
    "vlan_id": {"type": "integer", "description": "The access VLAN id (mode 'access')."},
}


NETWORK_PORTGROUP_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["created", "invalid_vlan_spec", "timeout"],
            "description": (
                "``'created'`` — the CreateDVPortgroup_Task reached success and the "
                "new portgroup's config was read back; ``'invalid_vlan_spec'`` — "
                "both vlan_trunk_ranges and vlan_id were passed (refused before any "
                "write); ``'timeout'`` — the task did not reach a terminal state "
                "within the poll bound (it may still complete). A task fault raises "
                "(the dispatcher wraps it connector_error)."
            ),
        },
        "portgroup": {
            "type": ["string", "null"],
            "description": (
                "The new portgroup's moid (from the task's TaskInfo.result MoRef); "
                "``null`` on a refusal / timeout."
            ),
        },
        "vds": {"type": "string", "description": "The DVS moid the portgroup was created on."},
        "name": {"type": "string", "description": "The requested portgroup name."},
        "task": {
            "type": ["string", "null"],
            "description": "The CreateDVPortgroup_Task moid (present once the write fired).",
        },
        "observed": {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": ["string", "null"], "description": "``config.name``."},
                "vlan": {
                    "type": ["object", "null"],
                    "properties": {
                        "_typeName": {
                            "type": "string",
                            "description": (
                                "``VmwareDistributedVirtualSwitchTrunkVlanSpec`` or "
                                "``VmwareDistributedVirtualSwitchVlanIdSpec``."
                            ),
                        },
                        "inherited": {
                            "type": "boolean",
                            "description": "Whether the VLAN comes from the switch.",
                        },
                        "vlanId": {
                            "type": ["integer", "array"],
                            "items": {"type": "object"},
                            "description": (
                                "One VLAN id, or ``NumericRange`` entries ``{start, end}``."
                            ),
                        },
                    },
                    "description": "``config.defaultPortConfig.vlan`` as the switch reports it.",
                },
            },
            "description": (
                "Read-back verification: the created portgroup's config.name and the "
                "raw config.defaultPortConfig.vlan spec the switch reports. ``null`` "
                "when no portgroup moid was resolved."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Next-step hint on a non-``created`` status; ``null`` on success.",
        },
    },
    "required": ["status", "vds", "name"],
}


NETWORK_PORTGROUP_SECURITY_SET_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["updated", "no_change_requested", "timeout"],
            "description": (
                "``'updated'`` — the ReconfigureDVPortgroup_Task reached success and "
                "the applied policy was read back; ``'no_change_requested'`` — no "
                "boolean was supplied (refused before any write); ``'timeout'`` — "
                "the task did not reach a terminal state within the poll bound. A "
                "task fault raises (the dispatcher wraps it connector_error)."
            ),
        },
        "portgroup": {"type": "string", "description": "The portgroup moid whose policy was set."},
        "requested": {
            "type": "object",
            "properties": _PORTGROUP_SECURITY_FIELDS,
            "description": (
                "The three security booleans as requested; a ``null`` field was not "
                "changed (only supplied booleans are written)."
            ),
        },
        "previous": {
            "type": ["object", "null"],
            "properties": _PORTGROUP_SECURITY_FIELDS,
            "description": (
                "Read-back of the effective security triple BEFORE the change (the "
                "four-eyes reviewer's before-state); ``null`` on a no-change refusal."
            ),
        },
        "observed": {
            "type": ["object", "null"],
            "properties": _PORTGROUP_SECURITY_FIELDS,
            "description": (
                "Read-back of the effective security triple AFTER the change; "
                "``null`` on a refusal / timeout."
            ),
        },
        "task": {
            "type": ["string", "null"],
            "description": "The ReconfigureDVPortgroup_Task moid (present once the write fired).",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Next-step hint on a non-``updated`` status; ``null`` on success.",
        },
    },
    "required": ["status", "portgroup", "requested"],
}


NETWORK_PORTGROUP_VLAN_SET_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["set", "unchanged", "invalid_vlan_spec", "timeout"],
            "description": (
                "``'set'`` -- the ReconfigureDVPortgroup_Task reached success and the "
                "applied VLAN was read back; ``'unchanged'`` -- the current VLAN config "
                "already equals the requested spec (normalised), so no task was issued; "
                "``'invalid_vlan_spec'`` -- both/neither VLAN mode, or replace=false "
                "(refused before any read/write); ``'timeout'`` -- the task did not reach "
                "a terminal state within the poll bound. A task fault raises (the "
                "dispatcher wraps it connector_error)."
            ),
        },
        "portgroup": {"type": "string", "description": "The portgroup moid whose VLAN was set."},
        "requested": {
            "type": ["object", "null"],
            "properties": _PORTGROUP_VLAN_VIEW_FIELDS,
            "description": (
                "The requested VLAN config as a normalised view "
                "(``{mode:'trunk', ranges:[{start,end}]}`` or ``{mode:'access', vlan_id}``); "
                "``null`` on an invalid-spec refusal."
            ),
        },
        "before": {
            "type": ["object", "null"],
            "properties": _PORTGROUP_VLAN_VIEW_FIELDS,
            "description": (
                "Read-back of the portgroup's VLAN config BEFORE the change (the "
                "four-eyes reviewer's before-state), normalised; ``null`` on a refusal or "
                "when the current spec could not be parsed (inherited / PVLAN)."
            ),
        },
        "after": {
            "type": ["object", "null"],
            "properties": _PORTGROUP_VLAN_VIEW_FIELDS,
            "description": (
                "Read-back of the VLAN config AFTER the change, normalised; equals "
                "``before`` on ``'unchanged'``; ``null`` on a refusal / timeout."
            ),
        },
        "task": {
            "type": ["string", "null"],
            "description": "The ReconfigureDVPortgroup_Task moid (present once the write fired).",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Next-step hint on a non-``set`` status; ``null`` on success.",
        },
    },
    "required": ["status", "portgroup"],
}


# ---------------------------------------------------------------------------
# Hardware write composites (#2891) -- parameter + response schemas
# ---------------------------------------------------------------------------
#
# Three pure-REST hardware writes from the #2859 provisioning flow:
# reconfigure CPU/memory (a freshly-cloned VM is stuck at the template's
# sizing), repoint a vNIC to a different distributed portgroup, and
# remove/edit/disconnect a CD-ROM (a template shipping a host-local-ISO
# CD-ROM pins every clone to one host and blocks vMotion). Each is
# ``dangerous`` / ``requires_approval=True`` like the other write
# composites.


#: ``vmware.composite.vm.resize`` parameter schema.
#:
#: Reads current sizing, then PATCHes CPU and/or memory. At least one of
#: ``cpu_count`` / ``cores_per_socket`` / ``memory_mib`` must be present
#: (``anyOf``). A change a powered-on VM cannot take live (no hot-add, a
#: decrease, or any cores_per_socket change) returns
#: ``status='requires_power_off'`` rather than a raw vCenter 400.
VM_RESIZE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "VM moid to resize.",
        },
        "cpu_count": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "New total vCPU count. Omit to leave CPU count unchanged. "
                "On a powered-on VM an increase requires CPU hot-add; a "
                "decrease requires power off (surfaced as "
                "``status='requires_power_off'``, never a raw 400)."
            ),
        },
        "cores_per_socket": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "New cores-per-socket. The total vCPU count must be a "
                "multiple of this value. Not hot-changeable -- a change on "
                "a powered-on VM returns ``status='requires_power_off'``."
            ),
        },
        "memory_mib": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "New memory size in MiB (maps to the vSphere ``size_MiB`` "
                "field). Omit to leave memory unchanged. On a powered-on VM "
                "an increase requires memory hot-add; a decrease requires "
                "power off (``status='requires_power_off'``)."
            ),
        },
    },
    "required": ["vm"],
    "anyOf": [
        {"required": ["cpu_count"]},
        {"required": ["cores_per_socket"]},
        {"required": ["memory_mib"]},
    ],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "cpu_count": 4, "memory_mib": 8192}],
}


#: ``vmware.composite.vm.nic.repoint`` parameter schema.
#:
#: Repoints an existing vNIC onto a distributed **or** host-scoped standard
#: portgroup, resolved by display name via
#: ``GET:/vcenter/network?filter.types=<backing_type>`` (there is no
#: dedicated portgroup list resource -- the #1602 reconciliation lesson).
#: ``backing_type`` defaults to ``DISTRIBUTED_PORTGROUP`` (back-compatible).
#: An explicit ``network`` moid skips name resolution. Ambiguous / missing
#: names refuse the repoint.
VM_NIC_REPOINT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "VM moid owning the vNIC.",
        },
        "nic": {
            "type": "string",
            "minLength": 1,
            "description": "vNIC device id (e.g. ``4000``) to repoint.",
        },
        "portgroup_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the target portgroup, resolved to its "
                "network moid via "
                "``GET:/vcenter/network?filter.types=<backing_type>``. "
                "A name matching zero portgroups returns "
                "``status='not_found'``; more than one returns "
                "``status='ambiguous'`` with the candidates listed (for "
                "host-scoped standard portgroups a name routinely matches "
                "one moid per host -- pass ``network`` to pick one). Still "
                "the human label the approver sees even when ``network`` is "
                "supplied."
            ),
        },
        "backing_type": {
            "type": "string",
            "enum": ["DISTRIBUTED_PORTGROUP", "STANDARD_PORTGROUP"],
            "default": "DISTRIBUTED_PORTGROUP",
            "description": (
                "Network kind the NIC is repointed onto. "
                "``DISTRIBUTED_PORTGROUP`` (default) targets a vDS "
                "portgroup; ``STANDARD_PORTGROUP`` targets a host-local "
                "standard-switch portgroup (e.g. to repair a NIC that "
                "landed on an L2 that cannot reach its gateway). Drives "
                "both the ``filter.types`` resolution scope and the PATCHed "
                "``backing.type``."
            ),
        },
        "network": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Explicit target ``Network`` moid. Skips name resolution "
                "and is validated for existence + type-consistency with "
                "``backing_type`` (a mismatch returns "
                "``status='invalid_request'``). Required to disambiguate a "
                "host-scoped standard portgroup whose name resolves to "
                "several moids (``status='ambiguous'``)."
            ),
        },
    },
    "required": ["vm", "nic", "portgroup_name"],
    "additionalProperties": False,
    "examples": [
        {
            "vm": "vm-42",
            "nic": "4000",
            "portgroup_name": "<portgroup-name>",
            "backing_type": "DISTRIBUTED_PORTGROUP",
        }
    ],
}


#: ``vmware.composite.vm.device.cdrom`` parameter schema.
#:
#: Verb-driven CD-ROM edit: ``remove`` (DELETE the device), ``update``
#: (PATCH its backing -- requires ``backing``), or ``disconnect`` (POST
#: ``?action=disconnect``; the device stays but the guest sees it
#: unplugged). The resolution read surfaces the current backing (the
#: host-local ISO path the approver needs to see) into the preview.
VM_DEVICE_CDROM_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "VM moid owning the CD-ROM device.",
        },
        "cdrom": {
            "type": "string",
            "minLength": 1,
            "description": "CD-ROM device id (e.g. ``16000``).",
        },
        "action": {
            "type": "string",
            "enum": ["remove", "update", "disconnect"],
            "description": (
                "``remove`` -> ``DELETE:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}``; "
                "``update`` -> ``PATCH`` the backing (requires ``backing``); "
                "``disconnect`` -> ``POST ?action=disconnect`` (the device "
                "stays, the guest sees it unplugged)."
            ),
        },
        "backing": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["ISO_FILE", "HOST_DEVICE", "CLIENT_DEVICE"],
                    "description": (
                        "Backing kind (``Cdrom.BackingType``). ``CLIENT_DEVICE`` "
                        "un-pins the VM from a host-local ISO."
                    ),
                },
                "iso_file": {
                    "type": "string",
                    "description": (
                        "Datastore path of the ISO, like ``[datastore] dir/image.iso``. "
                        "Only with ``type='ISO_FILE'``."
                    ),
                },
                "host_device": {
                    "type": "string",
                    "description": "Host CD-ROM device name. Only with ``type='HOST_DEVICE'``.",
                },
                "device_access_type": {
                    "type": "string",
                    "enum": ["EMULATION", "PASSTHRU", "PASSTHRU_EXCLUSIVE"],
                    "description": (
                        "How the guest reaches a physical or client device. Only "
                        "with ``HOST_DEVICE`` or ``CLIENT_DEVICE``."
                    ),
                },
            },
            "required": ["type"],
            "additionalProperties": True,
            "description": (
                "New CD-ROM backing spec when ``action='update'`` (e.g. "
                '``{"type": "CLIENT_DEVICE"}`` to un-pin a host-local '
                "ISO backing). Ignored for ``remove`` / ``disconnect``."
            ),
        },
    },
    "required": ["vm", "cdrom", "action"],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "cdrom": "16000", "action": "update", "backing": {"type": "CLIENT_DEVICE"}}
    ],
}


#: ``vmware.composite.vm.resize`` response schema.
VM_RESIZE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["resized", "requires_power_off", "no_change", "partial"],
            "description": (
                "``'resized'`` -- every requested dimension applied; "
                "``'requires_power_off'`` -- the VM is powered on and the "
                "change cannot be made live (no hot-add, a decrease, or a "
                "cores_per_socket change); ``'no_change'`` -- the requested "
                "values already match current; ``'partial'`` -- CPU applied "
                "but the memory PATCH failed."
            ),
        },
        "vm": {"type": "string", "description": "VM moid the resize targeted."},
        "name": {
            "type": ["string", "null"],
            "description": "VM display name read from ``GET:/vcenter/vm/{vm}``.",
        },
        "power_state": {
            "type": ["string", "null"],
            "description": "VM power state at read time (e.g. ``POWERED_ON`` / ``POWERED_OFF``).",
        },
        "applied": {
            "type": "object",
            "properties": {
                "cpu": {"type": "boolean"},
                "memory": {"type": "boolean"},
            },
            "required": ["cpu", "memory"],
            "description": "Which dimensions the composite actually PATCHed.",
        },
        "from": {
            "type": "object",
            "properties": {
                "cpu_count": {"type": ["integer", "null"]},
                "cores_per_socket": {"type": ["integer", "null"]},
                "memory_MiB": {"type": ["integer", "null"]},
            },
            "description": "Current sizing read before the change.",
        },
        "to": {
            "type": "object",
            "properties": {
                "cpu_count": {"type": ["integer", "null"]},
                "cores_per_socket": {"type": ["integer", "null"]},
                "memory_MiB": {"type": ["integer", "null"]},
            },
            "description": "Requested sizing (``null`` for dimensions left unchanged).",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Next-step hint on ``requires_power_off`` / ``partial``; ``null`` otherwise."
            ),
        },
    },
    "required": ["status", "vm", "applied", "from", "to"],
}


#: ``vmware.composite.vm.nic.repoint`` response schema.
VM_NIC_REPOINT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["repointed", "not_found", "ambiguous", "invalid_request"],
            "description": (
                "``'repointed'`` -- the NIC backing was PATCHed to the "
                "target portgroup; ``'not_found'`` -- no portgroup matched "
                "``portgroup_name`` (or the explicit ``network`` moid did "
                "not resolve); ``'ambiguous'`` -- more than one matched "
                "(candidates listed -- pass ``network`` to pick one); "
                "``'invalid_request'`` -- the explicit ``network`` moid is "
                "the wrong network kind for ``backing_type``. No PATCH is "
                "issued on any non-``repointed`` status."
            ),
        },
        "vm": {"type": "string", "description": "VM moid owning the NIC."},
        "nic": {"type": "string", "description": "vNIC device id repointed."},
        "mac_address": {
            "type": ["string", "null"],
            "description": (
                "NIC MAC address read from ``GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}``."
            ),
        },
        "current_backing": {
            "type": ["object", "null"],
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "STANDARD_PORTGROUP / HOST_DEVICE / DISTRIBUTED_PORTGROUP / OPAQUE_NETWORK."
                    ),
                },
                "network": {"type": ["string", "null"], "description": "Network moid."},
                "network_name": {"type": ["string", "null"], "description": "Network name."},
                "distributed_switch_uuid": {"type": ["string", "null"]},
                "distributed_port": {"type": ["string", "null"]},
            },
            "description": "The NIC's backing before the repoint (type, network, network_name).",
        },
        "requested_backing": {
            "type": "object",
            "properties": {
                "portgroup_id": {"type": ["string", "null"]},
                "portgroup_name": {"type": "string"},
                "backing_type": {
                    "type": "string",
                    "enum": ["DISTRIBUTED_PORTGROUP", "STANDARD_PORTGROUP"],
                },
            },
            "required": ["portgroup_id", "portgroup_name", "backing_type"],
            "description": (
                "The target portgroup the reviewer approves: ``backing_type`` "
                "(network kind), ``portgroup_id`` (resolved moid, ``null`` "
                "when resolution failed), and ``portgroup_name`` (display "
                "label)."
            ),
        },
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "network": {"type": "string", "description": "Network moid."},
                    "name": {"type": "string", "description": "Network name."},
                    "type": {"type": "string", "description": "Network type."},
                },
            },
            "description": (
                "Matching portgroup rows when ``status='ambiguous'`` / "
                "``'invalid_request'`` (empty otherwise)."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Disambiguation hint on ``not_found`` / ``ambiguous``; ``null`` otherwise."
            ),
        },
    },
    "required": ["status", "vm", "nic", "requested_backing"],
}


#: ``vmware.composite.vm.device.cdrom`` response schema.
VM_DEVICE_CDROM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["removed", "updated", "disconnected", "invalid_request", "result_unknown"],
            "description": (
                "``'removed'`` -- the device was deleted; ``'updated'`` -- "
                "its backing was PATCHed; ``'disconnected'`` -- the device "
                "was disconnected in-guest; ``'invalid_request'`` -- "
                "``action='update'`` without a ``backing`` object (no write "
                "issued); ``'result_unknown'`` -- the change timed out and "
                "may still run (see ``running_tasks`` and ``vm_question``)."
            ),
        },
        "vm": {"type": "string", "description": "VM moid owning the CD-ROM."},
        "cdrom": {"type": "string", "description": "CD-ROM device id acted on."},
        "action": {
            "type": "string",
            "enum": ["remove", "update", "disconnect"],
            "description": "The verb requested.",
        },
        "current_backing": {
            "type": ["object", "null"],
            "properties": {
                "type": {
                    "type": "string",
                    "description": "ISO_FILE / HOST_DEVICE / CLIENT_DEVICE.",
                },
                "iso_file": {"type": ["string", "null"], "description": "ISO datastore path."},
                "host_device": {"type": ["string", "null"]},
                "auto_detect": {"type": ["boolean", "null"]},
                "device_access_type": {"type": ["string", "null"]},
            },
            "description": (
                "The CD-ROM backing read before the change (the host-local "
                "ISO path the approver needs to see)."
            ),
        },
        "state": {
            "type": ["string", "null"],
            "description": (
                "Connection state at read time (e.g. ``CONNECTED`` / ``NOT_CONNECTED``)."
            ),
        },
        "requested_backing": {
            "type": ["object", "null"],
            "properties": {
                "type": {
                    "type": "string",
                    "description": "ISO_FILE / HOST_DEVICE / CLIENT_DEVICE.",
                },
                "iso_file": {"type": ["string", "null"], "description": "ISO datastore path."},
                "host_device": {"type": ["string", "null"]},
                "auto_detect": {"type": ["boolean", "null"]},
                "device_access_type": {"type": ["string", "null"]},
            },
            "description": "Echoed target backing when ``action='update'``; ``null`` otherwise.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Hint on ``invalid_request`` / ``result_unknown``, else ``null``.",
        },
        "running_tasks": {
            "type": "array",
            "description": "On ``result_unknown``: the queued or running vCenter tasks on the VM.",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task moid."},
                    "operation": {"type": ["string", "null"], "description": "What it does."},
                    "state": {"type": ["string", "null"], "description": "queued / running."},
                    "progress": {"type": ["integer", "null"], "description": "Percent."},
                    "start_time": {"type": ["string", "null"], "description": "Start time."},
                },
            },
        },
        "tasks_error": {
            "type": ["string", "null"],
            "description": (
                "On ``result_unknown``: why the running tasks could not be read, else "
                "``null``. ``vm_question`` is still valid when only the task read failed."
            ),
        },
        "vm_question": {
            "type": ["object", "null"],
            "description": (
                "On ``result_unknown``: the question the VM waits on (``id``, ``text``, "
                "``choices``, ``default_choice``, ``messages``), or ``null``. Answer it "
                "with ``vmware.vm.question.answer``."
            ),
        },
        "issues": {
            "type": "array",
            "description": "On ``result_unknown``: one ``error`` entry.",
            "items": {"type": "object"},
        },
    },
    "required": ["status", "vm", "cdrom", "action"],
}


# ===========================================================================
# Guest customization (GOSC) composites (#2892)
# ===========================================================================
#
# Two write composites cover guest OS customization -- how a cloned VM
# gets its hostname, per-NIC static IP + gateway + DNS, and (on Windows)
# its sysprep identity on first boot. Both are dangerous / approval-gated.
#
# Secret hygiene (#1503) is load-bearing here: the create schema carries
# Windows admin / product-key / domain-join credentials, and those values
# must never reach a reviewer, preview, broadcast, or audit surface. The
# op is pinned ``credential_write`` in ``broadcast/events.py`` (params
# collapse to aggregate-only on the feed) and its park-time preview
# builder echoes IDENTITY fields only (``_write_preview``); the durable
# audit row stores only a params hash. See the connector doc for the
# full three-surface argument.


#: ``vmware.composite.guest.customization_spec.create`` parameter schema.
#:
#: Creates a reusable named GuestOS customization spec via
#: ``POST:/vcenter/guest/customization-specs``. The agent-facing shape is
#: the tractable provisioning subset (hostname + per-NIC static IP +
#: DNS, linux or windows/sysprep) -- not the full vendor
#: ``CustomizationSpec`` surface. The handler maps it onto the vCenter
#: ``CreateSpec`` (``{name, description, spec}``) whose ``spec`` is a
#: ``CustomizationSpec`` (``{configuration_spec, interfaces,
#: global_dns_settings}``).
#:
#: The ``windows_*`` credential fields (``windows_admin_password`` /
#: ``windows_product_key`` / ``windows_domain_admin_password``) are
#: SECRET: they are consumed by the handler into the sysprep body but
#: never echoed onto any reviewer / preview / broadcast / audit surface
#: (#1503). The op is pinned ``credential_write`` so the broadcast feed
#: collapses these params to aggregate-only.
GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name for the new customization spec (``CreateSpec.name``). "
                "This is the name a later ``vmware.composite.vm.customize`` "
                "(or a clone's ``guest_customization_spec``) references."
            ),
        },
        "description": {
            "type": "string",
            "default": "",
            "description": "Free-text description stored on the spec (``CreateSpec.description``).",
        },
        "os_type": {
            "type": "string",
            "enum": ["linux", "windows"],
            "description": (
                "Guest OS: ``linux`` builds ``configuration_spec.linux_config`` and uses "
                "``domain`` / ``time_zone``; ``windows`` builds a sysprep "
                "``configuration_spec.windows_config`` and uses the ``windows_*`` fields. "
                "Fields of the other OS are ignored."
            ),
        },
        "hostname": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Guest host name. Mapped to a FIXED "
                "``HostnameGenerator`` (``{type: FIXED, fixed_name: "
                "<hostname>}``) on ``linux_config.hostname`` / the Windows "
                "``user_data.computer_name``."
            ),
        },
        "domain": {
            "type": "string",
            "default": "",
            "description": (
                "Linux only: DNS domain of the guest (``linux_config.domain``). "
                "Ignored for windows."
            ),
        },
        "time_zone": {
            "type": "string",
            "description": (
                "Linux only: guest time zone as a tz-database name, e.g. ``Europe/Vienna``. "
                "Ignored for windows (use ``windows_time_zone``). Left out when absent."
            ),
        },
        "interfaces": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ip_address": {
                        "type": "string",
                        "description": (
                            "Static IPv4 address for this NIC. Omit the "
                            "field to configure the NIC for DHCP instead."
                        ),
                    },
                    "prefix": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 32,
                        "description": (
                            "Subnet prefix length of the static address. Used only with "
                            "``ip_address``."
                        ),
                    },
                    "gateways": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Default gateway IPs of this NIC. Used only with ``ip_address``."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            "default": [],
            "description": (
                "Per-NIC IP settings in adapter order. Each entry maps to a "
                "``CustomizationSpec.interfaces`` ``AdapterMapping`` "
                "(``{adapter: {ipv4: {type, ip_address, prefix, gateways}}}``). "
                "An entry with no ``ip_address`` yields a DHCP adapter; the "
                "empty list leaves adapters unconfigured."
            ),
        },
        "dns_servers": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Global DNS server IPs (``global_dns_settings.dns_servers``).",
        },
        "dns_suffix_list": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Global DNS search suffixes (``global_dns_settings.dns_suffix_list``).",
        },
        "windows_admin_password": {
            "type": "string",
            "description": (
                "Windows only. Secret: password of the local Administrator "
                "(``windows_config.sysprep.gui_unattended.password``). MEHO never shows it in a "
                "preview, a broadcast or an audit row."
            ),
        },
        "windows_product_key": {
            "type": "string",
            "description": (
                "Windows only. Secret: Windows product key "
                "(``windows_config.sysprep.user_data.product_key``). MEHO never shows it in a "
                "preview, a broadcast or an audit row."
            ),
        },
        "windows_organization": {
            "type": "string",
            "description": "Windows only: registered organization (``user_data.organization``).",
        },
        "windows_full_name": {
            "type": "string",
            "description": "Windows only: registered owner name (``user_data.full_name``).",
        },
        "windows_join_domain": {
            "type": "string",
            "description": (
                "Windows only: Active Directory domain the guest joins "
                "(``sysprep.domain.domain`` with ``sysprep.domain.type = DOMAIN``)."
            ),
        },
        "windows_domain_admin_username": {
            "type": "string",
            "description": (
                "Windows only: domain account used for the join "
                "(``sysprep.domain.domain_username``). Used only with ``windows_join_domain``."
            ),
        },
        "windows_domain_admin_password": {
            "type": "string",
            "description": (
                "Windows only. Secret: password of the domain-join account "
                "(``sysprep.domain.domain_password``). Used only with ``windows_join_domain``. "
                "MEHO never shows it in a preview, a broadcast or an audit row."
            ),
        },
        "windows_auto_logon": {
            "type": "boolean",
            "default": False,
            "description": (
                "Windows only: whether the guest logs on by itself once after customization "
                "(sets ``gui_unattended.auto_logon_count`` to 1, else 0)."
            ),
        },
        "windows_time_zone": {
            "type": "integer",
            "default": 85,
            "description": (
                "Windows only: guest time zone as a Microsoft time-zone index "
                "(``gui_unattended.time_zone``), not the Linux ``time_zone`` name. Defaults to "
                "``85`` (GMT). See https://support.microsoft.com/help/973627."
            ),
        },
    },
    "required": ["spec_name", "os_type", "hostname"],
    "additionalProperties": False,
    "examples": [
        {
            "spec_name": "<spec-name>",
            "os_type": "linux",
            "hostname": "<hostname>",
            "domain": "example.com",
            "interfaces": [{"ip_address": "192.0.2.10", "prefix": 24, "gateways": ["192.0.2.1"]}],
            "dns_servers": ["192.0.2.53"],
        }
    ],
}


#: ``vmware.composite.guest.customization_spec.create`` response schema.
GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["created"],
            "description": (
                "``'created'`` -- the spec was created via "
                "``POST:/vcenter/guest/customization-specs``. Transport / "
                "vCenter faults surface as the dispatcher's "
                "``connector_error`` rather than a business status."
            ),
        },
        "spec_name": {
            "type": "string",
            "description": (
                "Name of the created spec -- echoed back so the caller can "
                "chain it into ``vmware.composite.vm.customize`` or a "
                "clone's ``guest_customization_spec``."
            ),
        },
        "os_type": {
            "type": "string",
            "enum": ["linux", "windows"],
            "description": "The guest OS branch the spec was built for.",
        },
    },
    "required": ["status", "spec_name", "os_type"],
}


#: ``vmware.composite.vm.customize`` parameter schema.
#:
#: Applies a saved customization spec to a VM (resolved by display name)
#: via ``PUT:/vcenter/vm/{vm}/guest/customization``. vCenter only accepts
#: a pending customization on a powered-off VM; the composite pre-checks
#: the resolved power state and refuses a powered-on VM with a structured
#: ``precondition_failed`` status rather than letting the PUT 400.
VM_CUSTOMIZE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "VM display name. Resolved via "
                "``GET:/vcenter/vm?filter.names=...`` to the moid + power "
                "state; multiple matches return ``status='ambiguous'``."
            ),
        },
        "spec_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the saved customization spec to apply "
                "(``Customization.SetSpec.name``). Typically the "
                "``spec_name`` from a prior "
                "``guest.customization_spec.create``."
            ),
        },
        "power_on": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, power the VM on via "
                "``POST:/vcenter/vm/{vm}/power?action=start`` after setting "
                "the pending customization, so it applies on that boot. "
                "Default false leaves the VM powered off."
            ),
        },
    },
    "required": ["name", "spec_name"],
    "additionalProperties": False,
    "examples": [{"name": "<vm-name>", "spec_name": "<spec-name>", "power_on": True}],
}


#: ``vmware.composite.vm.customize`` response schema.
VM_CUSTOMIZE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "customization_set",
                "powered_on",
                "not_found",
                "ambiguous",
                "precondition_failed",
            ],
            "description": (
                "``'customization_set'`` -- the pending customization was "
                "set (VM left powered-off); ``'powered_on'`` -- set then "
                "powered on so it applies this boot; ``'not_found'`` / "
                "``'ambiguous'`` -- name did not resolve to exactly one VM; "
                "``'precondition_failed'`` -- the VM is powered on and must "
                "be powered off first."
            ),
        },
        "vm": {
            "type": ["string", "null"],
            "description": "Resolved VM moid; ``null`` on ``not_found`` / ``ambiguous``.",
        },
        "name": {"type": "string", "description": "VM display name the caller supplied."},
        "spec_name": {"type": "string", "description": "Customization spec name applied."},
        "power_state": {
            "type": ["string", "null"],
            "description": (
                "Resolved power state at dispatch time (``POWERED_ON`` / "
                "``POWERED_OFF`` / ``SUSPENDED``); ``null`` when the VM did "
                "not resolve."
            ),
        },
        "applies_on": {
            "type": ["string", "null"],
            "description": (
                "``'next_power_on'`` once the customization is set; ``null`` when nothing was set."
            ),
        },
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string", "description": "VM moid."},
                    "name": {"type": "string", "description": "VM display name."},
                    "power_state": {"type": "string", "description": "VM power state."},
                },
            },
            "description": "The VMs that matched the name when ``status='ambiguous'``.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Human-readable next step on a non-success status; ``null`` otherwise.",
        },
    },
    "required": ["status", "name", "spec_name"],
}


#: ``vmware.composite.vm.deploy_from_library`` parameter schema.
#:
#: Deploys an OVF/OVA content-library item to a new VM via the synchronous
#: ``POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy``. The
#: library item is referenced either by ``library_item`` (id passthrough) or
#: by ``library_item_name`` (resolved via ``POST:/content/library/item?action=find``,
#: optionally scoped by ``library_name`` through
#: ``POST:/content/library?action=find``) with ambiguity refused before any
#: deploy. ``resource_pool`` is the one required placement (the vendor's
#: ``DeploymentTarget.resource_pool_id`` is required); ``host`` / ``folder`` /
#: ``datastore`` refine it. ``network_mappings`` is the OVF-network-key →
#: portgroup-moid map the OVF descriptor's NetworkSection identifiers key
#: into (spec: ``ResourcePoolDeploymentSpec.network_mappings`` is a map, not
#: an array).
VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_item": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Content-library OVF/OVA item id (a UUID). Rides the deploy "
                "path as ``POST:/vcenter/ovf/library-item/{ovfLibraryItemId}"
                "?action=deploy``. Give exactly one of ``library_item`` and "
                "``library_item_name``; both or neither is refused before the call."
            ),
        },
        "library_item_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "OVF/OVA item display name, resolved to an id via "
                "``POST:/content/library/item?action=find`` (filtered to "
                "``type='ovf'``). A name matching no item returns "
                "``status='item_not_found'``, more than one "
                "``status='ambiguous_item'`` with the candidate ids — no deploy "
                "fires. Give it instead of ``library_item``, never with it."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional library display name that scopes the "
                "``library_item_name`` lookup to one library. Resolved to a "
                "library id via ``POST:/content/library?action=find``; an unknown "
                "name returns ``status='library_not_found'`` and an ambiguous one "
                "``status='ambiguous_library'`` before any item lookup."
            ),
        },
        "resource_pool": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Target ``ResourcePool`` moid — the deploy target's required "
                "``resource_pool_id``. When it is a stand-alone host or a "
                "DRS-enabled cluster the server picks the host itself unless "
                "``host`` is pinned."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional target ``HostSystem`` moid (``DeploymentTarget.host_id``). "
                "Must be a member of the cluster owning ``resource_pool``."
            ),
        },
        "folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional destination VM ``Folder`` moid "
                "(``DeploymentTarget.folder_id``); the server chooses one if absent."
            ),
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional default ``Datastore`` moid "
                "(``ResourcePoolDeploymentSpec.default_datastore_id``) for OVF "
                "storage sections without an explicit mapping."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional display name for the deployed VM; the OVF descriptor's "
                "default name is used when omitted (read from the OVF summary before "
                "the deploy; a descriptor without a name is refused with "
                "``invalid_reference``). While the deploy runs the VM carries a "
                "unique deploy name (``<name>-meho-<token>``, cut to vCenter's "
                "80-character limit) and is renamed to this name once the deploy "
                "succeeds. A failed deploy finds and deletes only the VM "
                "(or vApp, with its VMs) that carries its own deploy name, so an "
                "older or another caller's same-name VM is never touched."
            ),
        },
        "network_mappings": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Map of OVF NetworkSection identifier → target ``Network`` moid "
                "(portgroup). Sent verbatim as "
                "``ResourcePoolDeploymentSpec.network_mappings`` (a map in the "
                "pinned 9.0 spec). Keys the OVF descriptor does not declare come "
                "back as a structured ``deploy_failed`` issue, not a raw fault."
            ),
        },
        "storage_provisioning": {
            "type": "string",
            "enum": ["thin", "thick", "eagerZeroedThick"],
            "description": (
                "Optional default disk provisioning for all OVF storage sections "
                "(``ResourcePoolDeploymentSpec.storage_provisioning``)."
            ),
        },
        "storage_profile": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional default ``StorageProfile`` id "
                "(``ResourcePoolDeploymentSpec.storage_profile_id``)."
            ),
        },
        "ovf_properties": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Optional OVF product-section properties as ``{property_id: value}``. "
                "Folded into a single ``PropertyParams`` entry in "
                "``ResourcePoolDeploymentSpec.additional_parameters``. Property "
                "values may be secret (e.g. appliance passwords), so the "
                "park-time preview echoes only the property **ids**, never the "
                "values."
            ),
        },
        "deployment_option": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional OVF deployment option key, for example the size profile "
                "of an appliance. Sent as ``selected_key`` of a "
                "``DeploymentOptionParams`` entry in "
                "``ResourcePoolDeploymentSpec.additional_parameters``. MEHO first "
                "reads the OVF summary (``?action=filter``) and checks the key "
                "against the ``deployment_options`` it lists. A key the item does "
                "not offer is refused with ``status='invalid_reference'`` before "
                "any deploy, and ``candidates`` lists the offered keys. Omit it to "
                "deploy the OVF's default option."
            ),
        },
        "accept_all_eula": {
            "type": "boolean",
            "description": (
                "Whether to accept all EULAs declared by the OVF package "
                "(``ResourcePoolDeploymentSpec.accept_all_eula``). Defaults to "
                "``true`` — deploying a curated library item accepts its EULA; a "
                "package with an unaccepted EULA returns ``status='deploy_failed'``."
            ),
        },
        "power_on": {
            "type": "boolean",
            "description": (
                "Power the deployed VM on afterward via "
                "``POST:/vcenter/vm/{vm}/power?action=start`` (OVF deploy itself "
                "never powers on). Best-effort: a power-on fault leaves "
                "``status='deployed'`` with ``powered_on=false`` and an issue."
            ),
        },
    },
    "required": ["resource_pool"],
    "oneOf": [{"required": ["library_item"]}, {"required": ["library_item_name"]}],
    "additionalProperties": False,
    "examples": [
        {
            "library_item_name": "<ovf-item-name>",
            "library_name": "<library-name>",
            "resource_pool": "resgroup-8",
            "folder": "group-v42",
            "datastore": "datastore-15",
            "name": "<vm-name>",
            "network_mappings": {"<ovf-network-name>": "dvportgroup-42"},
        }
    ],
}


#: ``vmware.composite.vm.deploy_from_library`` response schema.
VM_DEPLOY_FROM_LIBRARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "deployed",
                "deploy_failed",
                "deploy_error",
                "invalid_reference",
                "library_not_found",
                "ambiguous_library",
                "item_not_found",
                "ambiguous_item",
                "resolve_error",
            ],
            "description": (
                "``'deployed'`` — the OVF deploy report returned "
                "``succeeded=true`` and a resource id; ``'deploy_failed'`` — the "
                "report returned ``succeeded=false`` (network-mapping / placement "
                "/ EULA / descriptor validation), with per-issue messages under "
                "``issues``; ``'deploy_error'`` — the deploy call itself faulted "
                "(HTTP 400/404 for invalid or missing placement resources), "
                "surfaced as a structured message rather than a raw vendor error; "
                "``'invalid_reference'`` — neither ``library_item`` nor "
                "``library_item_name`` was supplied; ``'library_not_found'`` / "
                "``'ambiguous_library'`` — the ``library_name`` lookup matched "
                "zero / many libraries; ``'item_not_found'`` / ``'ambiguous_item'`` "
                "— the ``library_item_name`` lookup matched zero / many items; "
                "``'resolve_error'`` — a content-library ``?action=find`` "
                "resolution call itself faulted (HTTP 4xx/5xx), surfaced as a "
                "structured message with the vCenter status carried in ``issues`` "
                "rather than a raw vendor error. ``'resolve_error'`` and "
                "``'invalid_reference'`` also cover the OVF summary read that "
                "supplies the default VM name when ``name`` is omitted or checks "
                "``deployment_option``; ``'invalid_reference'`` also means the "
                "``deployment_option`` key is not one the OVF item offers. "
                "Every non-``deployed`` status is reached before or without a "
                "successful mutation: when a failed deploy leaves a partial VM "
                "or vApp behind, the composite tries to remove it and reports the "
                "outcome in ``issues``."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": (
                "Deployed resource moid (``DeploymentResult.resource_id.id``); "
                "``null`` unless ``status='deployed'``."
            ),
        },
        "resource_type": {
            "type": ["string", "null"],
            "description": (
                "``'VirtualMachine'`` or ``'VirtualApp'`` "
                "(``DeploymentResult.resource_id.type``); ``null`` unless deployed."
            ),
        },
        "library_item_id": {
            "type": ["string", "null"],
            "description": (
                "The library-item id the deploy used — the passthrough id, or the "
                "id resolved from ``library_item_name``. ``null`` when resolution "
                "failed before deploy."
            ),
        },
        "powered_on": {
            "type": "boolean",
            "description": (
                "Whether the follow-on power-on succeeded. Always ``false`` when "
                "``power_on`` was not requested or the deploy did not succeed."
            ),
        },
        "issues": {
            "type": "array",
            "items": _ISSUE_ITEM_SCHEMA,
            "description": (
                "Per-issue projections ``{category, severity, message}`` drawn "
                "from the OVF deploy report (errors / warnings / information) or "
                "the resolution / power-on failure. Empty on a clean deploy. A "
                "failed deploy adds one ``category='cleanup'`` entry per partial VM "
                "or vApp it found: severity ``info`` when it was removed, "
                "``warning`` when it could not be removed, its removal waits for "
                "an approval request (named in the message; approving it runs "
                "only that removal), or the check itself faulted. A "
                "``category='rename'`` warning means the deploy succeeded but the "
                "VM still carries its deploy name; the message names both names "
                "and, for a parked rename, the approval request. Approving a "
                "waiting rename or power-on runs only that step on the deployed "
                "VM; it never deploys again."
            ),
        },
        "candidates": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Candidate ids on ``ambiguous_item`` (library-item ids) or "
                "``ambiguous_library`` (library ids); on ``invalid_reference`` for "
                "a ``deployment_option`` the item does not offer, the keys it does "
                "offer; ``null`` otherwise."
            ),
        },
    },
    "required": ["status", "vm_id", "powered_on", "issues"],
}


#: ``vmware.composite.vm.import_from_library`` parameter schema.
#:
#: The typed ``HttpNfcLease`` OVF import (#3229): the durable counterpart to
#: ``deploy_from_library`` for items whose transfer outruns a single HTTP
#: read-timeout (#3176). Same name-resolution surface as the deploy composite,
#: but ``datastore`` is **required** (the vim ``OvfManager.CreateImportSpec``
#: request requires the inventory-placement datastore MoRef, unlike the REST
#: deploy which can derive it) and ``accept_all_eula`` / ``storage_profile`` are
#: dropped (the ``OvfCreateImportSpecParams`` cisp does not carry them).
VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_item": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Content-library OVF/OVA item id (a UUID). Give exactly one of "
                "``library_item`` and ``library_item_name``; both or neither is "
                "refused before the call."
            ),
        },
        "library_item_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "OVF/OVA item display name, resolved to an id via "
                "``POST:/content/library/item?action=find`` (filtered to "
                "``type='ovf'``). No match returns ``status='item_not_found'``, more "
                "than one ``status='ambiguous_item'`` with candidate ids — no import "
                "fires. Give it instead of ``library_item``, never with it."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional library display name scoping the ``library_item_name`` "
                "lookup to one library (via ``POST:/content/library?action=find``); "
                "an unknown name returns ``status='library_not_found'`` and an "
                "ambiguous one ``status='ambiguous_library'``."
            ),
        },
        "resource_pool": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Target ``ResourcePool`` moid the entity is imported into "
                "(``ResourcePool.ImportVApp`` receiver + ``CreateImportSpec`` "
                "resourcePool)."
            ),
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Target ``Datastore`` moid for the imported inventory objects "
                "(``CreateImportSpec.datastore``). Required for the lease import — "
                "the vim import spec is built against an explicit datastore."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional target ``HostSystem`` moid (``ImportVApp.host``). For a "
                "stand-alone host or a DRS cluster it may be omitted."
            ),
        },
        "folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional destination VM ``Folder`` moid (``ImportVApp.folder``); the "
                "datacenter's VM root folder is used when omitted."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional display name for the imported VM "
                "(``OvfCreateImportSpecParams.entityName``); the OVF descriptor's "
                "product name is used when omitted."
            ),
        },
        "network_mappings": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Map of OVF NetworkSection identifier → target ``Network`` moid "
                "(portgroup). Folded into ``OvfCreateImportSpecParams.networkMapping``. "
                "A key the OVF descriptor does not declare comes back as a structured "
                "``deploy_failed`` issue, not a raw fault."
            ),
        },
        "storage_provisioning": {
            "type": "string",
            "enum": ["thin", "thick"],
            "description": (
                "Optional disk provisioning applied to all disks "
                "(``OvfCreateImportSpecParams.diskProvisioning``)."
            ),
        },
        "ovf_properties": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Optional OVF product-section properties as ``{property_id: value}``, "
                "folded into ``OvfCreateImportSpecParams.propertyMapping``. Values may "
                "be secret (appliance passwords), so the park-time preview echoes only "
                "the property **ids**, never the values."
            ),
        },
        "power_on": {
            "type": "boolean",
            "description": (
                "Power the imported VM on afterward via "
                "``POST:/vcenter/vm/{vm}/power?action=start`` (the import itself never "
                "powers on). Best-effort: a power-on fault leaves ``status='deployed'`` "
                "with ``powered_on=false`` and an issue."
            ),
        },
    },
    "required": ["resource_pool", "datastore"],
    "oneOf": [{"required": ["library_item"]}, {"required": ["library_item_name"]}],
    "additionalProperties": False,
    "examples": [
        {
            "library_item": "00000000-0000-0000-0000-000000000000",
            "resource_pool": "resgroup-8",
            "datastore": "datastore-15",
            "name": "<vm-name>",
            "storage_provisioning": "thin",
        }
    ],
}


#: ``vmware.composite.vm.import_from_library`` response schema.
VM_IMPORT_FROM_LIBRARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "deployed",
                "deploy_failed",
                "deploy_error",
                "invalid_reference",
                "library_not_found",
                "ambiguous_library",
                "item_not_found",
                "ambiguous_item",
                "resolve_error",
            ],
            "description": (
                "The ``deploy_from_library`` envelope family, reused so the "
                "import and deploy paths share one contract: ``'deployed'`` — the "
                "lease import completed and the VM was created; ``'deploy_failed'`` — "
                "``OvfManager.CreateImportSpec`` rejected the descriptor (bad network "
                "mapping / property / placement), with per-issue messages under "
                "``issues``; ``'deploy_error'`` — a vim control-plane call, the lease "
                "state, or a disk upload faulted (surfaced as a structured message, "
                "never a raw vendor fault); the resolution statuses "
                "(``'invalid_reference'`` / ``'library_not_found'`` / "
                "``'ambiguous_library'`` / ``'item_not_found'`` / ``'ambiguous_item'`` "
                "/ ``'resolve_error'``) are reached before any mutation, identical to "
                "the deploy composite."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": (
                "Imported VM moid (``HttpNfcLeaseInfo.entity``); ``null`` unless "
                "``status='deployed'``."
            ),
        },
        "resource_type": {
            "type": ["string", "null"],
            "description": (
                "``'VirtualMachine'`` or ``'VirtualApp'`` "
                "(``HttpNfcLeaseInfo.entity`` type); ``null`` unless imported."
            ),
        },
        "library_item_id": {
            "type": ["string", "null"],
            "description": (
                "The library-item id the import used — the passthrough id, or the id "
                "resolved from ``library_item_name``. ``null`` when resolution failed "
                "before import."
            ),
        },
        "powered_on": {
            "type": "boolean",
            "description": (
                "Whether the follow-on power-on succeeded. Always ``false`` when "
                "``power_on`` was not requested or the import did not succeed."
            ),
        },
        "issues": {
            "type": "array",
            "items": _ISSUE_ITEM_SCHEMA,
            "description": (
                "Per-issue projections ``{category, severity, message}`` from the OVF "
                "descriptor validation, lease fault, disk-upload fault, resolution, or "
                "power-on. Empty on a clean import with no descriptor warnings."
            ),
        },
        "transfer": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Disk file path in the OVF package."},
                    "device_id": {
                        "type": "string",
                        "description": "Lease device id the file went to.",
                    },
                    "size_bytes": {"type": "integer", "description": "Bytes sent."},
                },
            },
            "description": (
                "Per-disk transfer manifest ``{path, device_id, size_bytes}`` for the "
                "files streamed to the lease. Empty unless ``status='deployed'``."
            ),
        },
        "candidates": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Candidate ids on ``ambiguous_item`` (library-item ids) or "
                "``ambiguous_library`` (library ids); ``null`` otherwise."
            ),
        },
    },
    "required": ["status", "vm_id", "powered_on", "issues", "transfer"],
}


# ===========================================================================
# Host-domain write composites (#3182) -- dangerous / requires approval
# ===========================================================================


#: ``vmware.composite.host.datastore_mount_nfs`` parameter schema.
#:
#: Mount an NFS export as a datastore on a host via the **synchronous** vim
#: ``HostDatastoreSystem.CreateNasDatastore`` (builds a ``HostNasVolumeSpec``).
#: **Idempotent**: it reads the host's mounted datastores first, so re-mounting
#: an already-mounted export returns ``status="already_mounted"`` (no write, no
#: fault) — safe to retry. The host is selected by display name or moref;
#: ``access_mode`` / ``nfs_type`` map to the pinned spec's ``HostMountMode_enum``
#: / ``HostNasVolumeSpec.type``.
HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host display name or moref (e.g. 'esxi-01' or 'host-15'). "
                "Resolved via ``GET:/vcenter/host`` — a display-name lookup first, "
                "falling back to a moref match; an ambiguous name is refused with "
                "``status='ambiguous_host'`` before any write. **Optional / ignored "
                "on a standalone-ESXi target**: a target whose probe "
                "fingerprint is product=esxi has exactly one host (the target "
                "itself, the well-known ha-host), so ``host`` is not needed and is "
                "ignored. Required on a vCenter target (else ``status='host_required'``)."
            ),
        },
        "nfs_server": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Hostname or IP of the NFS v3 server exporting the share "
                "(``HostNasVolumeSpec.remoteHost``)."
            ),
        },
        "remote_path": {
            "type": "string",
            "minLength": 1,
            "description": "Exported remote path on the NFS server (``remotePath``).",
        },
        "datastore_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Local datastore name the mount is created under "
                "(``HostNasVolumeSpec.localPath``). If this name is already in "
                "use on the host for a **different** export the op returns "
                "``status='name_conflict'`` (a structured refusal, no write); an "
                "already-mounted export under any name returns "
                "``status='already_mounted'``."
            ),
        },
        "access_mode": {
            "type": "string",
            "enum": ["readWrite", "readOnly"],
            "default": "readWrite",
            "description": (
                "Datastore access mode (``HostMountMode_enum``). ``readOnly`` is "
                "for ISO / template stores a VM cannot power on from."
            ),
        },
        "nfs_type": {
            "type": "string",
            "enum": ["NFS", "NFS41"],
            "default": "NFS",
            "description": (
                "NAS volume type (``HostNasVolumeSpec.type``). ``NFS`` is v3; "
                "``NFS41`` is v4.1. CIFS is deliberately out of scope (credential-"
                "bearing)."
            ),
        },
    },
    "required": ["nfs_server", "remote_path", "datastore_name"],
    "additionalProperties": False,
    "examples": [
        {
            "host": "host-21",
            "nfs_server": "<nfs-server>",
            "remote_path": "/<export-path>",
            "datastore_name": "<datastore-name>",
            "nfs_type": "NFS41",
        }
    ],
}


#: ``vmware.composite.host.disk_mark_flash`` parameter schema.
#:
#: Present one or more host disks as flash (SSD) or non-flash (HDD) via vim
#: ``HostStorageSystem.MarkAsSsd_Task`` / ``MarkAsNonSsd_Task`` (task-polled).
#: Nested labs surface virtual disks as HDD; vSAN-ready bring-up validation
#: needs them flash-marked. Disks are named by ``scsiDiskUuid``.
HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host display name or moref (see host.datastore_mount_nfs). "
                "Optional / ignored on a standalone-ESXi target; required "
                "on a vCenter target."
            ),
        },
        "disk_uuids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "SCSI disk UUIDs (``ScsiLun.uuid``) to mark. One "
                "``MarkAs{Ssd,NonSsd}_Task`` is issued per disk and polled to a "
                "terminal state; per-disk outcomes are captured independently "
                "(a fault on one disk does not abort the rest)."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["flash", "non_flash"],
            "default": "flash",
            "description": (
                "``'flash'`` marks each disk as SSD (``MarkAsSsd_Task``); "
                "``'non_flash'`` is the inverse (``MarkAsNonSsd_Task``) — the same "
                "op keyed on this param, not a second op."
            ),
        },
    },
    "required": ["disk_uuids"],
    "additionalProperties": False,
    "examples": [{"host": "host-21", "disk_uuids": ["<scsi-disk-uuid>"], "mode": "flash"}],
}


#: ``vmware.composite.host.service_control`` parameter schema.
#:
#: Start / stop / restart a host service and optionally set its startup
#: policy via vim ``HostServiceSystem`` (synchronous). **Bounded to a
#: curated server-side allowlist**: an out-of-list ``service`` is refused
#: (``status='service_not_allowed'``) before any write — never passed
#: through to the host.
HOST_SERVICE_CONTROL_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host display name or moref (see host.datastore_mount_nfs). "
                "Optional / ignored on a standalone-ESXi target; required "
                "on a vCenter target."
            ),
        },
        "service": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host service key (``HostService.key``). Enforced server-side "
                "against a curated allowlist (``TSM-SSH`` / ``TSM`` / ``ntpd`` / "
                "``ptpd``); any other name is refused with "
                "``status='service_not_allowed'`` before any resolution or write."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["start", "stop", "restart"],
            "description": (
                "Lifecycle transition: ``StartService`` / ``StopService`` / "
                "``RestartService``. Applied before any policy update."
            ),
        },
        "policy": {
            "type": "string",
            "enum": ["on", "automatic", "off"],
            "description": (
                "Optional startup policy (``HostServicePolicy_enum``): ``on`` "
                "(start at host boot), ``automatic`` (start iff a firewall port is "
                "open), ``off`` (do not start). When supplied, "
                "``UpdateServicePolicy`` runs after the action."
            ),
        },
    },
    "required": ["service", "action"],
    "additionalProperties": False,
    "examples": [{"host": "host-21", "service": "TSM-SSH", "action": "start", "policy": "off"}],
}


#: ``vmware.composite.host.datastore_mount_nfs`` response schema.
HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": [
                "mounted",
                "already_mounted",
                "name_conflict",
                "host_not_found",
                "ambiguous_host",
                "config_manager_unreadable",
                "unsupported_host_target",
                "host_required",
            ],
            "description": (
                "``'mounted'`` — the datastore was created. ``'already_mounted'`` "
                "— the requested export ``(nfs_server, remote_path)`` is already "
                "mounted on the host (idempotent success, no write dispatched); "
                "``datastore`` is the existing moid. ``'name_conflict'`` — the "
                "requested ``datastore_name`` is in use for a **different** export "
                "(structured refusal, no write). The remaining refusal statuses "
                "are reached before any write (host name/moref did not resolve "
                "uniquely, the host's HostDatastoreSystem was unreadable, the "
                "target is neither vCenter nor standalone ESXi "
                "(``unsupported_host_target``), or a vCenter target was "
                "given no ``host`` (``host_required``))."
            ),
        },
        "host": {
            "type": ["string", "null"],
            "description": (
                "Resolved host moid (``ha-host`` on a standalone-ESXi target), or "
                "the input host on refusal (``null`` when none was supplied)."
            ),
        },
        "datastore": {
            "type": ["string", "null"],
            "description": (
                "Datastore moid: the new datastore on ``mounted``, the existing "
                "datastore on ``already_mounted`` / ``name_conflict``; ``null`` on "
                "the other refusal statuses."
            ),
        },
        "summary": {
            "type": ["object", "null"],
            "properties": {
                "datastore": {"type": ["string", "null"], "description": "Datastore moid."},
                "name": {"type": ["string", "null"], "description": "Datastore name."},
                "nfs_server": {
                    "type": ["string", "null"],
                    "description": "NFS server (``HostNasVolumeSpec.remoteHost``).",
                },
                "remote_path": {
                    "type": ["string", "null"],
                    "description": "Export path (``HostNasVolumeSpec.remotePath``).",
                },
                "access_mode": {
                    "type": ["string", "null"],
                    "enum": ["readWrite", "readOnly", None],
                    "description": "``HostMountMode_enum``; ``null`` on ``name_conflict``.",
                },
                "type": {
                    "type": ["string", "null"],
                    "description": "NAS volume type, like ``NFS`` or ``NFS41``.",
                },
            },
            "description": (
                "Datastore summary — datastore moid, name, and mount coordinates — "
                "on ``mounted`` / ``already_mounted`` (and the conflicting "
                "datastore's coordinates on ``name_conflict``); ``null`` on the "
                "other refusals."
            ),
        },
        "candidate_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Matched host moids on ``ambiguous_host``.",
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "host", "datastore"],
}


#: ``vmware.composite.host.disk_mark_flash`` response schema.
HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": [
                "marked",
                "partial",
                "host_not_found",
                "ambiguous_host",
                "config_manager_unreadable",
                "unsupported_host_target",
                "host_required",
            ],
            "description": (
                "``'marked'`` — every disk reached SSD/HDD state; ``'partial'`` — "
                "at least one disk faulted / timed out / errored (see per-disk "
                "``results``); the refusal statuses (incl. ``unsupported_host_target`` "
                "/ ``host_required``) are reached before any write."
            ),
        },
        "host": {
            "type": ["string", "null"],
            "description": (
                "Resolved host moid (``ha-host`` on a standalone-ESXi target), or "
                "the input host on refusal (``null`` when none was supplied)."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["flash", "non_flash"],
            "description": "The requested mode.",
        },
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "disk_uuid": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["marked", "faulted", "timeout", "error"],
                    },
                    "task": {"type": ["string", "null"]},
                    "error": {"type": ["string", "null"]},
                },
                "required": ["disk_uuid", "status"],
            },
            "description": "One row per disk in ``disk_uuids`` (empty on a pre-write refusal).",
        },
        "summary": {
            "type": ["object", "null"],
            "properties": {
                "marked": {"type": "integer", "minimum": 0},
                "failed": {"type": "integer", "minimum": 0},
            },
            "description": "Aggregate counts across ``results``; ``null`` on refusal.",
        },
        "candidate_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Matched host moids on ``ambiguous_host``.",
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "host", "mode", "results"],
}


#: ``vmware.composite.host.service_control`` response schema.
HOST_SERVICE_CONTROL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": [
                "applied",
                "service_not_allowed",
                "host_not_found",
                "ambiguous_host",
                "config_manager_unreadable",
                "unsupported_host_target",
                "host_required",
            ],
            "description": (
                "``'applied'`` — the action (and optional policy update) ran; "
                "``'service_not_allowed'`` — the service is outside the curated "
                "allowlist (refused before any resolution or write); the remaining "
                "refusals (incl. ``unsupported_host_target`` / ``host_required``) "
                "are reached before any write."
            ),
        },
        "host": {
            "type": ["string", "null"],
            "description": (
                "Resolved host moid (``ha-host`` on a standalone-ESXi target), or "
                "the input host on refusal (``null`` when none was supplied)."
            ),
        },
        "service": {"type": "string", "description": "The requested service key."},
        "action": {
            "type": ["string", "null"],
            "enum": ["start", "stop", "restart", None],
            "description": "The action that was asked for.",
        },
        "policy": {
            "type": ["string", "null"],
            "enum": ["on", "automatic", "off", None],
            "description": "The start policy that was set; ``null`` when none was given.",
        },
        "policy_updated": {
            "type": "boolean",
            "description": "Whether ``UpdateServicePolicy`` ran (``policy`` was supplied).",
        },
        "allowed_services": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The curated allowlist, echoed on ``service_not_allowed``.",
        },
        "candidate_hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Matched host moids on ``ambiguous_host``.",
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "host", "service", "policy_updated"],
}


# ===========================================================================
# Guest-operations channel (vmware.composite.vm.guest.*, #3100)
# ===========================================================================
#
# Guest OS credentials are NEVER a parameter of these ops: they resolve
# from the target's Vault ``secret_ref`` (the ``guest_username`` /
# ``guest_password`` fields). ``guest.process.list`` / ``guest.env.read`` /
# ``guest.net.show`` are ``safe`` reads; ``guest.file.read`` is a ``caution``
# read (#3720 -- its opt-in ``fetch_content=true`` returns arbitrary guest
# bytes read as the in-guest login, so it auto-parks for agent / service
# principals). ``guest.file.write`` (#3100) and ``guest.program.run`` (#3255)
# are the ``dangerous`` / ``requires_approval`` writes.


#: Optional per-call guest credential reference shared by the five guest ops
#: that log into the guest OS (#3364). A reference string only -- the value is
#: read in-process under the operator's identity and never enters params.
_GUEST_SECRET_REF_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Optional reference to the secret holding this VM's guest login "
        "(guest_username / guest_password), in the same form as a target's "
        "secret_ref (e.g. 'targets/app-vm-01', 'vault:targets/app-vm-01', "
        "'gsm:<project>/<secret>'). Omit to use the vCenter target's own "
        "secret. The reference must fall under the target's allowed "
        "guest-secret prefix (by default the namespace of the target's "
        "secret_ref, or extras.guest_secret_prefix); any other reference "
        "is rejected before any secret is read. Only the reference travels "
        "in params; the credential value never does."
    ),
}


#: ``vmware.composite.vm.guest.process.list`` parameter schema.
GUEST_PROCESS_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the VM whose guest processes to list (e.g. 'vm-42')."
            ),
        },
        "max_processes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 200,
            "description": (
                "Cap on the number of processes returned. Applied client-side "
                "after ListProcessesInGuest returns so a busy guest never floods "
                "the agent surface; the response ``count`` detects truncation."
            ),
        },
        "guest_ops_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "guestOperationsManager",
            "description": (
                "MoId of the top-level GuestOperationsManager singleton. The "
                "GuestProcessManager MoRef is resolved off it at call time. "
                "Override only if a deployment names the singleton differently."
            ),
        },
        "guest_secret_ref": _GUEST_SECRET_REF_PROPERTY,
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "max_processes": 100}],
}


#: ``vmware.composite.vm.guest.env.read`` parameter schema.
GUEST_ENV_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Managed-object ID of the VM whose guest environment to read.",
        },
        "names": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Specific environment-variable names to read. Omit to read the "
                "guest user's entire environment (ReadEnvironmentVariableInGuest "
                "returns every variable when the name list is empty)."
            ),
        },
        "guest_ops_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "guestOperationsManager",
            "description": (
                "MoId of the top-level GuestOperationsManager singleton (see process.list)."
            ),
        },
        "guest_secret_ref": _GUEST_SECRET_REF_PROPERTY,
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "names": ["PATH"]}],
}


#: ``vmware.composite.vm.guest.net.show`` parameter schema.
#:
#: Reads Tools-reported guest network state off the VM object -- no guest
#: credentials, nothing runs inside the guest.
GUEST_NET_SHOW_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the VM whose Tools-reported guest network "
                "state (per-NIC IPs, routes, DNS, gateways) to read."
            ),
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}


#: ``vmware.composite.vm.guest.file.read`` parameter schema.
GUEST_FILE_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Managed-object ID of the VM whose guest file to read.",
        },
        "guest_path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute path of the file inside the guest OS (e.g. "
                "'/etc/os-release'). The guest user must be able to read it."
            ),
        },
        "guest_ops_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "guestOperationsManager",
            "description": (
                "MoId of the top-level GuestOperationsManager singleton (see process.list)."
            ),
        },
        "fetch_content": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, MEHO fetches the file bytes server-side over the "
                "one-time transfer URL and returns them (see max_inline_bytes). "
                "When false (default) only the transfer metadata + URL is "
                "returned, exactly as before. The transfer URL and its token "
                "are never returned or logged when the bytes are fetched."
            ),
        },
        "max_inline_bytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 8388608,
            "default": 1048576,
            "description": (
                "Byte ceiling for fetch_content (default 1048576 = 1 MiB, hard "
                "cap 8388608 = 8 MiB). A file larger than this is refused with "
                "an error naming its size -- re-read without fetch_content for "
                "the transfer handle, or raise this value up to the hard cap. "
                "Ignored when fetch_content is false."
            ),
        },
        "guest_secret_ref": _GUEST_SECRET_REF_PROPERTY,
    },
    "required": ["vm", "guest_path"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "guest_path": "/etc/os-release", "fetch_content": True}],
}


#: ``vmware.composite.vm.guest.file.write`` parameter schema (the one write).
GUEST_FILE_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "Managed-object ID of the VM to write a file into.",
        },
        "guest_path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute destination path inside the guest OS (e.g. "
                "'/etc/systemd/network/10-mtu.conf'). The guest user must be "
                "able to write it."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "UTF-8 text content to write. Never a credential -- guest "
                "credentials resolve from the target's secret_ref, not params."
            ),
        },
        "overwrite": {
            "type": "boolean",
            "default": False,
            "description": (
                "Whether to overwrite an existing file at ``guest_path``. When "
                "false, the write fails if the file already exists (vSphere "
                "InitiateFileTransferToGuest semantics)."
            ),
        },
        "guest_ops_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "guestOperationsManager",
            "description": (
                "MoId of the top-level GuestOperationsManager singleton (see process.list)."
            ),
        },
        "guest_secret_ref": _GUEST_SECRET_REF_PROPERTY,
    },
    "required": ["vm", "guest_path", "content"],
    "additionalProperties": False,
    "examples": [
        {
            "vm": "vm-42",
            "guest_path": "/etc/<app-name>/<file-name>.conf",
            "content": "<file-content>",
            "overwrite": False,
        }
    ],
}


#: ``vmware.composite.vm.guest.process.list`` response schema.
GUEST_PROCESS_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM moid the processes were listed for."},
        "process_manager_moid": {
            "type": "string",
            "description": (
                "Resolved GuestProcessManager MoId the ListProcessesInGuest call targeted."
            ),
        },
        "processes": {
            "type": "array",
            "items": GUEST_PROCESS_INFO_SCHEMA,
            "description": (
                "GuestProcessInfo entries (name / pid / owner / cmdLine / "
                "startTime / endTime / exitCode), capped to ``max_processes``. "
                "Set-shaped: the dispatcher returns a JSONFlux result handle "
                "when the list is over threshold."
            ),
        },
        "count": {
            "type": "integer",
            "minimum": 0,
            "description": "Post-cap length of ``processes`` -- detects truncation.",
        },
        "max_processes_applied": {
            "type": "integer",
            "minimum": 1,
            "description": "Effective ``max_processes`` cap applied to the response.",
        },
    },
    "required": ["vm", "process_manager_moid", "processes", "count"],
}


#: ``vmware.composite.vm.guest.env.read`` response schema.
GUEST_ENV_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM moid the environment was read from."},
        "process_manager_moid": {
            "type": "string",
            "description": "Resolved GuestProcessManager MoId the call targeted.",
        },
        "variables": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Guest environment as ``NAME=value`` strings (whole environment "
                "when ``names`` was omitted, else the requested names). "
                "Set-shaped -- JSONFlux-wrapped over threshold."
            ),
        },
        "count": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of variables returned.",
        },
    },
    "required": ["vm", "process_manager_moid", "variables", "count"],
}


#: ``vmware.composite.vm.guest.net.show`` response schema.
GUEST_NET_SHOW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM moid the guest network state was read for."},
        "nics": {
            "type": "array",
            "items": GUEST_NIC_INFO_SCHEMA,
            "description": (
                "GuestNicInfo entries from ``guest.net`` -- per-NIC MAC, "
                "connected state, and IP addresses as Tools reports them."
            ),
        },
        "ip_stacks": {
            "type": "array",
            "items": GUEST_STACK_INFO_SCHEMA,
            "description": (
                "GuestStackInfo entries from ``guest.ipStack`` -- routing table, "
                "DNS config, and default gateways as Tools reports them."
            ),
        },
    },
    "required": ["vm", "nics", "ip_stacks"],
}


#: ``vmware.composite.vm.guest.file.read`` response schema.
GUEST_FILE_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM moid the file read was initiated on."},
        "file_manager_moid": {
            "type": "string",
            "description": (
                "Resolved GuestFileManager MoId the InitiateFileTransferFromGuest call targeted."
            ),
        },
        "guest_path": {"type": "string", "description": "Absolute guest path read."},
        "url": {
            "type": ["string", "null"],
            "description": (
                "One-time guest-transfer URL from InitiateFileTransferFromGuest. "
                "Returned when content_fetch='deferred' (fetch_content=false). "
                "Omitted when content_fetch='inline' -- the one-time URL is "
                "consumed by the server-side fetch and is never returned."
            ),
        },
        "size_bytes": {
            "type": ["integer", "null"],
            "description": "File size in bytes as reported by the guest file manager.",
        },
        "attributes": {
            **GUEST_FILE_ATTRIBUTES_SCHEMA,
            "description": (
                "GuestFileAttributes for the file (POSIX / Windows metadata). "
                "Present on a deferred read and on an inline read returned in "
                "full; dropped from the reduced summary when a large inline file "
                "spills content_lines to a result handle (the summary preserves "
                "only scalar identifying/size fields, not this dict) -- re-read "
                "with fetch_content=false to obtain the attributes for such a file."
            ),
        },
        "content_fetch": {
            "type": "string",
            "enum": ["deferred", "inline"],
            "description": (
                "'deferred' -- only transfer metadata + URL returned (default); "
                "'inline' -- file bytes were fetched and returned as content_lines."
            ),
        },
        "content_encoding": {
            "type": "string",
            "enum": ["utf-8", "base64"],
            "description": (
                "Present only when content_fetch='inline'. 'utf-8' -- emitted "
                "only when the whole file decodes cleanly as UTF-8; content_lines "
                "are its text lines (line-oriented: a trailing newline is dropped "
                "and a CRLF file keeps a dangling '\\r', so utf-8 lines are NOT "
                "byte-reversible). 'base64' -- content_lines are base64 chunks "
                "(used whenever the file is not clean UTF-8, e.g. binary); "
                "concatenate then base64-decode for the exact, byte-reversible "
                "content."
            ),
        },
        "content_lines": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Present only when content_fetch='inline'. The file content as "
                "a list of text lines (utf-8) or base64 chunks (binary). "
                "content_encoding says which; use base64 for byte-exact content. "
                "Set-shaped, so the dispatcher JSONFlux-wraps it into a result "
                "handle over the size threshold; drill in via result_query."
            ),
        },
    },
    "required": ["vm", "file_manager_moid", "guest_path"],
}


#: ``vmware.composite.vm.guest.file.write`` response schema.
GUEST_FILE_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["written"],
            "description": (
                "``'written'`` -- the transfer URL was minted and the bytes were "
                "PUT to the guest. (A parked / denied approval returns the "
                "governance OperationResult verbatim, not this envelope.)"
            ),
        },
        "vm": {"type": "string", "description": "VM moid the file was written to."},
        "file_manager_moid": {
            "type": "string",
            "description": (
                "Resolved GuestFileManager MoId the InitiateFileTransferToGuest call targeted."
            ),
        },
        "guest_path": {"type": "string", "description": "Absolute guest path written."},
        "size_bytes": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of UTF-8 content bytes written.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether an existing file was allowed to be overwritten.",
        },
    },
    "required": ["status", "vm", "guest_path", "size_bytes", "overwrite"],
}


#: ``vmware.composite.vm.guest.program.run`` parameter schema (#3255).
#:
#: The freeform in-guest program-execution write #3100 deferred. ``dangerous``
#: / ``requires_approval``. ``arguments`` and ``env`` values may carry secrets
#: (a password on a command line, a token in an env var); they are redacted
#: from the approval preview, never echoed into the result, and never logged
#: (guest credentials themselves resolve from ``secret_ref``, never params).
GUEST_PROGRAM_RUN_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of the VM whose guest OS runs the program (e.g. 'vm-42')."
            ),
        },
        "program_path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute path of the program to start inside the guest (vim "
                "GuestProgramSpec.programPath), e.g. '/usr/bin/systemctl' or a "
                "Windows 'powershell.exe'. On Linux/Solaris the program is "
                "launched via a guest shell."
            ),
        },
        "arguments": {
            "type": "string",
            "description": (
                "Command-line arguments for the program as a single shell string "
                "(vim GuestProgramSpec.arguments) -- e.g. '-Command "
                '"Install-WindowsFeature AD-Domain-Services"\'. On Linux/Solaris '
                "the guest shell interprets it (stdio redirection works; shell "
                "metacharacters must be escaped). Defaults to an empty string. "
                "MAY carry secrets: the value is redacted from the approval "
                "preview and never appears in the result, the audit row, or logs."
            ),
        },
        "working_directory": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute working directory for the program (vim "
                "GuestProgramSpec.workingDirectory). Omit to use the guest "
                "default (the auth user's home directory on Linux)."
            ),
        },
        "env": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Environment variables for the program, a NAME->value map "
                "serialised to vim GuestProgramSpec.envVariables 'NAME=value' "
                "entries. WARNING: vim REPLACES the guest's whole environment "
                "with this set -- it is not additive, so an incomplete map can "
                "drop PATH and system variables the program needs. Omit to "
                "inherit the guest default environment. MAY carry secrets: "
                "values are redacted from the approval preview and never appear "
                "in the result, the audit row, or logs (only the variable NAMES "
                "reach the reviewer)."
            ),
        },
        "wait": {
            "type": "boolean",
            "default": False,
            "description": (
                "When false (default), return as soon as the program is started "
                "-- StartProgramInGuest is fire-and-forget and yields only a PID "
                "(no output is captured). When true, poll ListProcessesInGuest "
                "until the process exits or ``timeout_seconds`` elapses, "
                "returning the exit code and start/end times."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3600,
            "default": 300,
            "description": (
                "Only used when ``wait`` is true: the wall-clock ceiling for the "
                "exit-code poll. VMware Tools keeps a finished process's exit "
                "code listable for only ~5 minutes after completion (and not "
                "across a Tools restart), so the default matches that window; a "
                "program running longer may return status 'exit_unknown' if its "
                "exit info ages out before a poll observes it."
            ),
        },
        "guest_ops_manager_moid": {
            "type": "string",
            "minLength": 1,
            "default": "guestOperationsManager",
            "description": (
                "MoId of the top-level GuestOperationsManager singleton (see process.list)."
            ),
        },
        "guest_secret_ref": _GUEST_SECRET_REF_PROPERTY,
    },
    "required": ["vm", "program_path"],
    "additionalProperties": False,
    "examples": [
        {
            "vm": "vm-42",
            "program_path": "/usr/bin/systemctl",
            "arguments": "restart <service-name>",
            "wait": True,
            "timeout_seconds": 120,
        }
    ],
}


#: ``vmware.composite.vm.guest.program.run`` response schema (#3255).
GUEST_PROGRAM_RUN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["started", "exited", "timeout", "exit_unknown"],
            "description": (
                "``'started'`` -- wait=false: the program was launched (PID "
                "returned, no exit code polled). ``'exited'`` -- wait=true: the "
                "process completed and its exit code was captured. ``'timeout'`` "
                "-- wait=true: ``timeout_seconds`` elapsed while the process was "
                "still running (exit_code null). ``'exit_unknown'`` -- wait=true: "
                "the process was no longer listable before an exit code was seen "
                "(it finished and aged out of VMware Tools' ~5-minute window, or "
                "Tools restarted); exit_code null. (A parked / denied approval "
                "returns the governance OperationResult verbatim, not this "
                "envelope.)"
            ),
        },
        "vm": {"type": "string", "description": "VM moid the program ran in."},
        "process_manager_moid": {
            "type": "string",
            "description": (
                "Resolved GuestProcessManager MoId the StartProgramInGuest call targeted."
            ),
        },
        "program_path": {
            "type": "string",
            "description": (
                "Absolute program path started (echoed; ``arguments`` / ``env`` "
                "are never echoed back)."
            ),
        },
        "pid": {
            "type": "integer",
            "description": "The process ID StartProgramInGuest returned.",
        },
        "exit_code": {
            "type": ["integer", "null"],
            "description": (
                "The process exit code when ``status='exited'``; null for "
                "'started' / 'timeout' / 'exit_unknown'."
            ),
        },
        "start_time": {
            "type": ["string", "null"],
            "description": "GuestProcessInfo.startTime (ISO-8601) when captured; else null.",
        },
        "end_time": {
            "type": ["string", "null"],
            "description": (
                "GuestProcessInfo.endTime (ISO-8601) when the process completed; else null."
            ),
        },
        "wait": {
            "type": "boolean",
            "description": "Whether the caller requested the exit-code poll.",
        },
    },
    "required": ["status", "vm", "process_manager_moid", "program_path", "pid", "wait"],
}


# ---------------------------------------------------------------------------
# namespace-management -- Supervisor (WCP) enable / disable / status (#3281)
# ---------------------------------------------------------------------------


#: ``vmware.composite.supervisor.enable`` parameter schema. The nested
#: ``control_plane`` / ``workloads`` objects are the vendor
#: ``EnableOnComputeClusterSpec`` sub-objects passed through to the enable POST
#: body verbatim, so they keep ``additionalProperties: True`` (the full vendor
#: spec -- LB config, DNS/NTP, image sync, CIDRs -- flows through unmodified).
#: The schema pins the *structural* essentials (management network + control-
#: plane storage policy, workload network_type + edge provider). The edge
#: provider has an enum (``Networks.Edges.EdgeProvider``, the same set as the
#: handler's ``_ALLOWED_EDGE_PROVIDERS``), so a wrong provider is refused before
#: the call. The handler still answers an unknown ``network_type`` with a loud
#: structured refusal (``unknown_network_provider``) rather than an opaque
#: vCenter 400.
SUPERVISOR_ENABLE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "ClusterComputeResource moid (``domain-cN``) of the vSphere cluster to "
                "enable the Supervisor on. Rides the ``{cluster}`` path segment of "
                "``POST /vcenter/namespace-management/supervisors/{cluster}"
                "?action=enable_on_compute_cluster`` (the current 9.x path; the "
                "``clusters/{cluster}?action=enable`` form is deprecated as of vSphere 9.0)."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "User-friendly Supervisor name (EnableOnComputeClusterSpec.name).",
        },
        "control_plane": {
            "type": "object",
            "required": ["network", "storage_policy"],
            "properties": {
                "network": {
                    "type": "object",
                    "description": (
                        "Management network for the control plane (Networks.Management."
                        "Network): backing portgroup + IP management (gateway in CIDR "
                        "form, 5-address node range) + DNS/NTP services."
                    ),
                },
                "storage_policy": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "SPBM policy id backing the Supervisor Kubernetes API server "
                        "(SpsStorageProfile). Required -- on NFS there is no default "
                        "policy; pass the tag-based NFS policy id."
                    ),
                },
                "size": {
                    "type": "string",
                    "enum": ["TINY", "SMALL", "MEDIUM", "LARGE"],
                    "description": (
                        "Control-plane size (vCenter ``Supervisors.ControlPlane.size``). "
                        "Optional; vCenter uses SMALL when it is left out. TINY = 8 GB per "
                        "control-plane VM."
                    ),
                },
                "count": {
                    "type": "integer",
                    "enum": [1, 3],
                    "description": "Number of control-plane VMs (1 or 3). Optional; defaults to 3.",
                },
            },
            "additionalProperties": True,
            "description": (
                "EnableOnComputeClusterSpec.control_plane -- passed through to the "
                "enable body. Requires a management ``network`` and a ``storage_policy``."
            ),
        },
        "workloads": {
            "type": "object",
            "required": ["network", "edge"],
            "properties": {
                "network": {
                    "type": "object",
                    "required": ["network_type"],
                    "properties": {
                        "network_type": {
                            "type": "string",
                            "description": (
                                "Workload network stack (Supervisors.Networks.Workload."
                                "NetworkType). One of VSPHERE (VDS + Foundation LB), "
                                "NSX_VPC (NSX VPC), NSXT (classic NSX-T). Any other "
                                "value gets status='unknown_network_provider' and "
                                "nothing changes."
                            ),
                        },
                    },
                    "additionalProperties": True,
                    "description": (
                        "Workload network (Supervisors.Networks.Workload.Network), passed "
                        "to vCenter as given. Needs ``network_type``; the other vCenter "
                        "fields (for example ``vsphere``, ``nsx_vpc``, ``ip_management``) "
                        "are optional."
                    ),
                },
                "edge": {
                    "type": "object",
                    "required": ["provider"],
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": [
                                "VSPHERE_FOUNDATION",
                                "NSX",
                                "NSX_VPC",
                                "NSX_ADVANCED",
                                "HAPROXY",
                            ],
                            "description": (
                                "Edge (load-balancer) provider (Networks.Edges."
                                "EdgeProvider). One of VSPHERE_FOUNDATION (pairs with the "
                                "VSPHERE stack -- no separate NSX edge cluster required), "
                                "NSX / NSX_VPC / NSX_ADVANCED, or the deprecated HAPROXY. "
                                "Any other value is refused before the call and nothing "
                                "changes."
                            ),
                        },
                    },
                    "additionalProperties": True,
                    "description": (
                        "Edge (load balancer) settings (Networks.Edges.Edge), passed to "
                        "vCenter as given. Needs ``provider``; the provider's own block "
                        "(for example ``foundation``) and ``load_balancer_address_ranges`` "
                        "are optional here."
                    ),
                },
            },
            "additionalProperties": True,
            "description": (
                "EnableOnComputeClusterSpec.workloads -- passed through to the enable "
                "body. Requires a workload ``network`` (with ``network_type``) and an "
                "``edge`` (with ``provider``); ``storage`` (ephemeral/image policies) is "
                "optional and defaults from the control plane when omitted."
            ),
        },
        "zone": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional pre-existing consumption fault-domain Zone id. Omitted -> a "
                "zone is auto-created from the cluster's managed-object id."
            ),
        },
    },
    "required": ["cluster", "name", "control_plane", "workloads"],
    "additionalProperties": False,
    "examples": [
        {
            "cluster": "domain-c7",
            "name": "<supervisor-name>",
            "control_plane": {
                "network": {"backing": {"backing": "NETWORK", "network": "dvportgroup-30"}},
                "storage_policy": "<storage-policy-id>",
                "size": "SMALL",
                "count": 3,
            },
            "workloads": {
                "network": {"network_type": "VSPHERE"},
                "edge": {"provider": "VSPHERE_FOUNDATION"},
            },
        }
    ],
}


#: ``vmware.composite.supervisor.enable`` response schema.
SUPERVISOR_ENABLE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["enabling", "unknown_network_provider", "invalid_spec"],
            "description": (
                "``'enabling'`` -- the enable POST was accepted (asynchronous; poll "
                "``vmware.composite.supervisor.status``). ``'unknown_network_provider'`` "
                "-- workloads.network.network_type or workloads.edge.provider is not a "
                "known 9.x value (refused before any write). ``'invalid_spec'`` -- a "
                "required control_plane / workloads sub-field was missing (refused before "
                "any write)."
            ),
        },
        "cluster": {"type": "string", "description": "Input cluster moid."},
        "supervisor": {
            "type": ["string", "null"],
            "description": (
                "New Supervisor id returned by the enable POST; ``null`` on any refusal."
            ),
        },
        "network_type": {
            "type": ["string", "null"],
            "description": "Echoed workload network_type on success / provider refusal.",
        },
        "edge_provider": {
            "type": ["string", "null"],
            "description": "Echoed edge provider on success.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next: poll supervisor.status, or why it was refused.",
        },
    },
    "required": ["status", "cluster", "supervisor"],
}


#: ``vmware.composite.supervisor.disable`` parameter schema.
SUPERVISOR_DISABLE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "ClusterComputeResource moid of the Supervisor-enabled cluster to tear "
                "down. Rides ``POST /vcenter/namespace-management/clusters/{cluster}"
                "?action=disable`` (``DELETE clusters/{cluster}`` 404s)."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7"}],
}


#: ``vmware.composite.supervisor.disable`` response schema.
SUPERVISOR_DISABLE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["disabling"],
            "description": (
                "``'disabling'`` -- the disable POST was accepted (asynchronous; poll "
                "``vmware.composite.supervisor.status``, config_status moves through "
                "'REMOVING')."
            ),
        },
        "cluster": {"type": "string", "description": "Input cluster moid."},
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next (poll supervisor.status).",
        },
    },
    "required": ["status", "cluster"],
}


#: ``vmware.composite.supervisor.status`` parameter schema.
SUPERVISOR_STATUS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "ClusterComputeResource moid to read Supervisor status for "
                "(``GET /vcenter/namespace-management/clusters/{cluster}``)."
            ),
        },
        "messages_limit": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Optional cap on the inline ``messages`` / ``conditions`` arrays "
                "(default 25). ``message_count`` / ``condition_count`` always carry the "
                "uncapped sizes."
            ),
        },
    },
    "required": ["cluster"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7", "messages_limit": 10}],
}


#: ``vmware.composite.supervisor.status`` response schema. Shaped for inline
#: polling: the scalar ``config_status`` / ``kubernetes_status`` / ``ready``
#: stay top-level so a runbook OperationCallVerify step or a Sensor assertion
#: reads them without a JSONFlux handle.
SUPERVISOR_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "Input cluster moid."},
        "config_status": {
            "type": ["string", "null"],
            "description": (
                "Clusters.ConfigStatus: CONFIGURING / REMOVING / RUNNING / ERROR. "
                "``null`` when absent from the vCenter payload."
            ),
        },
        "kubernetes_status": {
            "type": ["string", "null"],
            "description": "Clusters.KubernetesStatus: READY / WARNING / ERROR; else null.",
        },
        "ready": {
            "type": "boolean",
            "description": (
                "True iff config_status == 'RUNNING' AND kubernetes_status == 'READY' -- "
                "the single poll predicate a runbook / Sensor waits on."
            ),
        },
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": "INFO, WARNING or ERROR.",
                    },
                    "details": {
                        "type": "object",
                        "description": "The message text (vCenter localizable message).",
                    },
                    "kb_article_link": {
                        "type": "string",
                        "description": "Link to a knowledge base article, if any.",
                    },
                    "id": {"type": "string", "description": "Message id, if any."},
                },
            },
            "description": "Capped Clusters.Message rows, as vCenter returns them.",
        },
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "What the condition is about."},
                    "status": {"type": "string", "description": "Condition status."},
                    "severity": {"type": "string", "description": "Condition severity."},
                    "reason": {"type": "string", "description": "Short reason code."},
                    "description": {
                        "type": "object",
                        "description": "Condition text (vCenter localizable message).",
                    },
                    "last_transition_time": {
                        "type": "string",
                        "description": "When the status last changed.",
                    },
                    "messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Messages of this condition.",
                    },
                },
            },
            "description": "Capped Clusters.Condition rows, as vCenter returns them.",
        },
        "message_count": {"type": "integer", "description": "Uncapped message count."},
        "condition_count": {"type": "integer", "description": "Uncapped condition count."},
    },
    "required": ["cluster", "config_status", "kubernetes_status", "ready"],
}


# ===========================================================================
# storage_policy.* — governed NFS tag-based SPBM policy create/delete (#3494)
# ===========================================================================

#: ``vmware.composite.storage_policy.list`` parameter schema.
#:
#: Optional ``policy_ids`` narrows the list; absent, every visible policy is
#: returned (JSONFlux-reduced by the dispatcher when set-shaped).
STORAGE_POLICY_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policy_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Optional storage-policy identifiers to filter the list by. "
                "Absent or empty lists every visible policy."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
    "examples": [{"policy_ids": ["<policy-id>"]}],
}

#: ``vmware.composite.storage_policy.list`` response schema.
STORAGE_POLICY_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policies": {
            "type": "array",
            "description": "The visible storage policies (Vcenter.Storage.Policies.Summary rows).",
            "items": {
                "type": "object",
                "properties": {
                    "policy": {"type": "string", "description": "Storage-policy identifier."},
                    "name": {"type": "string", "description": "Storage-policy display name."},
                    "description": {"type": "string", "description": "Storage-policy description."},
                },
                "required": ["policy", "name"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["policies"],
    "additionalProperties": True,
}

#: ``vmware.composite.storage_policy.create`` parameter schema.
#:
#: Mints a tag-based requirement storage policy for NFS-principal datastores
#: (which have no default policy). Ensures the tag category + tag, attaches the
#: tag to each named datastore, then mints the PBM policy whose one rule
#: requires that tag. Idempotent (#3826): an existing category / tag / policy of
#: the same name is adopted (reported in ``adopted`` / ``issues``) so a retry
#: after a partial run succeeds instead of failing at ``ALREADY_EXISTS``; an
#: incompatible existing category or a same-named policy with a different rule
#: set is a terminal ``category_conflict`` / ``policy_conflict``.
STORAGE_POLICY_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policy_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": (
                "Display name of the storage policy to create (PBM caps the name at 80 characters)."
            ),
        },
        "category_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the tag category to create. The tag rule "
                "references this category; the property id is "
                "``com.vmware.storage.tag.<category_name>.property``."
            ),
        },
        "tag_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the tag to create in the category and attach "
                "to the datastores. The policy's tag rule requires this tag."
            ),
        },
        "datastore_names": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "Names of the datastore(s) the tag is attached to (typically "
                "the NFS datastore(s) the Supervisor / VKS storage binds to). "
                "Each name must resolve to exactly one datastore."
            ),
        },
        "description": {
            "type": "string",
            "description": "Optional description applied to the category, tag, and policy.",
        },
    },
    "required": ["policy_name", "category_name", "tag_name", "datastore_names"],
    "additionalProperties": False,
    "examples": [
        {
            "policy_name": "<policy-name>",
            "category_name": "<category-name>",
            "tag_name": "<tag-name>",
            "datastore_names": ["<datastore-name>"],
        }
    ],
}

#: ``vmware.composite.storage_policy.create`` response schema.
STORAGE_POLICY_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "created",
                "adopted",
                "datastore_not_found",
                "category_conflict",
                "policy_conflict",
                "policy_create_failed",
            ],
            "description": (
                "``created`` — policy minted and visible (category / tag may "
                "have been adopted, see ``adopted``); ``adopted`` — a policy of "
                "the same name and matching rule set already existed and was "
                "reused (no PbmCreate); ``datastore_not_found`` — a datastore "
                "name resolved to zero / many datastores (no tag substrate "
                "created); ``category_conflict`` — a category of the same name "
                "exists but with an incompatible cardinality / associable types "
                "(no write); ``policy_conflict`` — a policy of the same name "
                "exists but constrains a different rule set (no write); "
                "``policy_create_failed`` — PbmCreate did not return a policy id "
                "(retry: the tag substrate is adopted, no cleanup needed)."
            ),
        },
        "policy_id": {
            "type": ["string", "null"],
            "description": (
                "The created or adopted PBM policy id (the vCenter StoragePolicy identifier)."
            ),
        },
        "policy_name": {"type": "string", "description": "The requested policy name."},
        "category_id": {
            "type": ["string", "null"],
            "description": "The created or adopted tag category id.",
        },
        "tag_id": {
            "type": ["string", "null"],
            "description": "The created or adopted tag id.",
        },
        "tag_name": {"type": "string", "description": "The requested tag name."},
        "datastores": {
            "type": "array",
            "description": "Resolved datastores the tag was attached to.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Datastore name."},
                    "moid": {"type": "string", "description": "Datastore moid."},
                },
                "required": ["name", "moid"],
                "additionalProperties": False,
            },
        },
        "listed": {
            "type": "boolean",
            "description": "Whether the read-back GET saw the new policy in the policies list.",
        },
        "adopted": {
            "type": "object",
            "description": (
                "Per sub-step, whether an existing object was reused rather than "
                "created (so a second run after a partial run goes on)."
            ),
            "properties": {
                "category": {"type": "boolean", "description": "The tag category existed."},
                "tag": {"type": "boolean", "description": "The tag existed."},
                "policy": {"type": "boolean", "description": "The policy existed."},
            },
            "required": ["category", "tag", "policy"],
            "additionalProperties": False,
        },
        "issues": {
            "type": "array",
            "description": (
                "Structured issue entries: a ``warning`` per adopted sub-step, "
                "or an ``error`` on a terminal ``*_conflict`` / "
                "``policy_create_failed`` envelope (dispatched as an error)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                    "message": {"type": "string"},
                },
                "required": ["category", "severity", "message"],
                "additionalProperties": False,
            },
        },
        "guidance": {"type": ["string", "null"], "description": "Next-step hint."},
    },
    "required": ["status", "policy_id", "policy_name"],
    "additionalProperties": True,
}

#: ``vmware.composite.storage_policy.delete`` parameter schema.
STORAGE_POLICY_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policy_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The storage-policy id to delete (the PBM profile uniqueId / "
                "the identifier storage_policy.list returns as ``policy``)."
            ),
        },
        "policy_name": {
            "type": "string",
            "minLength": 1,
            "description": "Optional display name, echoed in guidance for the approver.",
        },
    },
    "required": ["policy_id"],
    "additionalProperties": False,
    "examples": [{"policy_id": "<policy-id>", "policy_name": "<policy-name>"}],
}

#: ``vmware.composite.storage_policy.delete`` response schema.
STORAGE_POLICY_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["deleted", "delete_failed", "still_present"],
            "description": (
                "``deleted`` — PbmDelete reported no per-id fault and the "
                "read-back no longer lists the id; ``delete_failed`` — PbmDelete "
                "returned a per-id fault (e.g. the policy is in use); "
                "``still_present`` — PbmDelete reported success but the "
                "read-back still lists the id."
            ),
        },
        "policy_id": {"type": "string", "description": "The policy id from the call."},
        "fault": {
            "type": ["string", "null"],
            "description": "The per-id PbmDelete fault type, when status='delete_failed'.",
        },
        "guidance": {"type": ["string", "null"], "description": "Next-step hint."},
    },
    "required": ["status", "policy_id"],
    "additionalProperties": True,
}


# ===========================================================================
# Governed vSphere Namespace create/delete composites (#3502)
# ===========================================================================


#: ``vmware.composite.namespace.create`` parameter schema.
#:
#: The ``Namespaces.Instances.CreateSpecV2`` body: ``supervisor`` (the enabled
#: Supervisor id, #3281) + ``namespace`` (the DNS-1123 name) are required; the
#: ``access_list`` / ``storage_specs`` (SPBM policy ids from #3494) /
#: ``vm_service_spec`` (content-library ids from #3495 + VM classes) are
#: optional pass-through sub-objects with vendor sub-fields left open
#: (``additionalProperties: True``), so a valid CreateSpecV2 is not rejected by
#: an over-tight schema before it reaches vCenter.
#: Namespace labels are interpolated into a REST path for status/delete and
#: are the CreateSpecV2 ``namespace`` field for create.  The negative
#: lookahead is a portable end assertion: unlike ``$``, it does not accept a
#: trailing newline as a valid complete input.
_NAMESPACE_DNS_LABEL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 63,
    "pattern": r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?![\s\S])",
}

NAMESPACE_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supervisor": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Supervisor id the namespace is created on (CreateSpecV2.supervisor): "
                "the id supervisor.enable returns."
            ),
        },
        "namespace": {
            **_NAMESPACE_DNS_LABEL_SCHEMA,
            "description": (
                "vSphere Namespace name (a DNS-1123 label; becomes the Kubernetes "
                "namespace a VKS guest cluster is created into)."
            ),
        },
        "access_list": {
            "type": "array",
            "description": (
                "Namespaces.Instances.Access entries: who may use the namespace, with "
                "which role. Optional; passed to vCenter as given."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "subject_type": {
                        "type": "string",
                        "enum": ["USER", "GROUP"],
                        "description": "USER for one user, GROUP for a group of users.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "User or group name, without the domain.",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Domain of the user or group, e.g. ``vsphere.local``.",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["OWNER", "EDIT", "VIEW"],
                        "description": (
                            "OWNER may change and delete the namespace, EDIT may change it, "
                            "VIEW may only read it."
                        ),
                    },
                    "identity_provider": {
                        "type": "string",
                        "description": (
                            "Id of an external identity provider for the subject. Leave it "
                            "out to use vCenter Single Sign-On."
                        ),
                    },
                },
                "additionalProperties": True,
            },
        },
        "storage_specs": {
            "type": "array",
            "description": (
                "Namespaces.Instances.StorageSpec entries: the storage policies the "
                "namespace may use. Optional; passed to vCenter as given."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "policy": {
                        "type": "string",
                        "description": (
                            "Storage policy id, for example the one storage_policy.create "
                            "returns. vCenter makes a Kubernetes storage class for it."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Most storage (MiB) the namespace may use with this policy. "
                            "Leave it out for no limit."
                        ),
                    },
                },
                "additionalProperties": True,
            },
        },
        "vm_service_spec": {
            "type": "object",
            "description": (
                "Namespaces.Instances.VMServiceSpec: the content libraries and VM "
                "classes the namespace may use. Optional; passed to vCenter as given."
            ),
            "properties": {
                "content_libraries": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Content library ids, for example the Kubernetes release library "
                        "from content_library.subscribed.create."
                    ),
                },
                "vm_classes": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "VM class names that exist on the Supervisor, e.g. ``best-effort-small``."
                    ),
                },
            },
            "additionalProperties": True,
        },
    },
    "required": ["supervisor", "namespace"],
    "additionalProperties": False,
    "examples": [
        {
            "supervisor": "<supervisor-id>",
            "namespace": "example-ns",
            "access_list": [
                {
                    "subject_type": "USER",
                    "subject": "<user-name>",
                    "domain": "vsphere.local",
                    "role": "EDIT",
                }
            ],
            "storage_specs": [{"policy": "<storage-policy-id>"}],
            "vm_service_spec": {
                "content_libraries": ["<content-library-id>"],
                "vm_classes": ["best-effort-small"],
            },
        }
    ],
}

#: ``vmware.composite.namespace.create`` response schema.
NAMESPACE_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["created"],
            "description": (
                "``created`` -- the create POST was accepted (asynchronous; the "
                "namespace converges CONFIGURING -> RUNNING)."
            ),
        },
        "namespace": {"type": "string", "description": "The created namespace name."},
        "supervisor": {"type": "string", "description": "The Supervisor id it was created on."},
        "config_status": {
            "type": ["string", "null"],
            "description": (
                "Read-back Namespaces.Instances.InfoV2.config_status "
                "(CONFIGURING / RUNNING / ERROR); ``null`` when the read-back is not "
                "yet visible or faulted."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next (poll namespace.status).",
        },
    },
    "required": ["status", "namespace", "supervisor"],
    "additionalProperties": True,
}

#: ``vmware.composite.namespace.delete`` parameter schema.
NAMESPACE_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {
            **_NAMESPACE_DNS_LABEL_SCHEMA,
            "description": (
                "vSphere Namespace name to delete. Rides the ``{namespace}`` path "
                "segment of DELETE /vcenter/namespaces/instances/{namespace}. "
                "Deleting a namespace cascades -- it destroys every workload inside "
                "it (VKS guest clusters, pods, PVCs)."
            ),
        },
    },
    "required": ["namespace"],
    "additionalProperties": False,
    "examples": [{"namespace": "example-ns"}],
}

#: ``vmware.composite.namespace.delete`` response schema.
NAMESPACE_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["deleted", "removing", "still_present"],
            "description": (
                "``deleted`` -- the read-back GET 404s (the namespace is gone); "
                "``removing`` -- the read-back reports config_status='REMOVING' "
                "(delete accepted, asynchronous teardown draining); "
                "``still_present`` -- the DELETE was accepted but the read-back "
                "still reports a non-REMOVING status."
            ),
        },
        "namespace": {"type": "string", "description": "The namespace name."},
        "config_status": {
            "type": ["string", "null"],
            "description": "Read-back config_status when the namespace is still present.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next; ``null`` when the namespace is gone.",
        },
    },
    "required": ["status", "namespace"],
    "additionalProperties": True,
}

#: ``vmware.composite.namespace.status`` parameter schema. Reads a single
#: namespace by name; ``messages_limit`` caps the inline ``messages`` array.
NAMESPACE_STATUS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {
            **_NAMESPACE_DNS_LABEL_SCHEMA,
            "description": (
                "vSphere Namespace name to read status for (rides the "
                "``{namespace}`` segment of "
                "GET /vcenter/namespaces/instances/v2/{namespace})."
            ),
        },
        "messages_limit": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Optional cap on the inline ``messages`` array (default 25). "
                "``message_count`` always carries the uncapped size."
            ),
        },
    },
    "required": ["namespace"],
    "additionalProperties": False,
    "examples": [{"namespace": "example-ns", "messages_limit": 10}],
}

#: ``vmware.composite.namespace.status`` response schema. Shaped for inline
#: polling: the scalar ``config_status`` / ``ready`` / ``exists`` stay
#: top-level so a runbook OperationCallVerify step or a Sensor assertion reads
#: them without a JSONFlux handle -- the governed, boot-enabled poll op the
#: namespace.create / .delete composites hand the caller (symmetric with
#: ``vmware.composite.supervisor.status``).
NAMESPACE_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string", "description": "Input namespace name."},
        "exists": {
            "type": "boolean",
            "description": (
                "False when the read-back GET 404s (namespace not yet visible "
                "mid-create, or gone after delete) -- a normal poll answer, not "
                "a fault; the other fields are then null / empty."
            ),
        },
        "config_status": {
            "type": ["string", "null"],
            "description": (
                "Namespaces.Instances.ConfigStatus: CONFIGURING / REMOVING / "
                "RUNNING / ERROR. ``null`` when the namespace is absent or the "
                "field is missing from the vCenter payload."
            ),
        },
        "ready": {
            "type": "boolean",
            "description": (
                "True iff config_status == 'RUNNING' -- the single poll "
                "predicate a runbook / Sensor waits on before creating a VKS "
                "guest cluster into the namespace."
            ),
        },
        "stats": {
            "type": ["object", "null"],
            "properties": {
                "cpu_used": {"type": "integer", "description": "CPU used by pods, in MHz."},
                "memory_used": {"type": "integer", "description": "Memory used by pods, in MiB."},
                "storage_used": {
                    "type": "integer",
                    "description": "Storage used by pods, in MiB.",
                },
            },
            "description": "Namespaces.Instances.Stats; ``null`` when absent.",
        },
        "description": {
            "type": ["string", "null"],
            "description": "The namespace description; ``null`` when absent.",
        },
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": "INFO, WARNING or ERROR.",
                    },
                    "details": {
                        "type": "object",
                        "description": "The message text (vCenter localizable message).",
                    },
                },
            },
            "description": "Capped Namespaces.Instances.Message rows, as vCenter returns them.",
        },
        "message_count": {"type": "integer", "description": "Uncapped message count."},
    },
    "required": ["namespace", "exists", "config_status", "ready"],
}


# ===========================================================================
# Content-library SUBSCRIBED-library composites (#3495)
# ===========================================================================


#: ``vmware.composite.content_library.subscribed.create`` parameter schema.
#:
#: Create a SUBSCRIBED content library subscribed to a remote publisher (the
#: TKr/VKr image source for a vSphere Supervisor). The subscription
#: ``password`` may be secret; it rides the request body but never a preview /
#: broadcast / audit-hash surface (the op is pinned into
#: ``_CREDENTIAL_WRITE_OPS`` and carries a bespoke identity-only preview).
CONTENT_LIBRARY_SUBSCRIBED_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name for the new subscribed library "
                "(``Content.LibraryModel.name``). Names need not be unique."
            ),
        },
        "subscription_url": {
            "type": "string",
            "minLength": 1,
            "description": (
                "URL of the publisher's library file "
                "(``Content.Library.SubscriptionInfo.subscription_url``), for example the "
                "public VMware Kubernetes release library "
                "``https://wp-content.vmware.com/v2/latest/lib.json``."
            ),
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the datastore backing the library "
                "(``Content.Library.StorageBacking`` of type ``DATASTORE``), "
                "resolved to a moid via ``GET:/vcenter/datastore`` "
                "(``filter.names``, exact match). A name matching no datastore "
                "returns ``status='datastore_not_found'``, more than one "
                "``status='ambiguous_datastore'`` — no library is created. A "
                "storage backing is required even for an on-demand subscribed "
                "library (only item metadata syncs, but the backing is still "
                "mandatory)."
            ),
        },
        "on_demand": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (default), only item **metadata** synchronises; each "
                "item's content (files) is pulled on first use. Recommended for "
                "a TKr library — the ~3 GB-per-release image content then pulls "
                "lazily at guest-cluster-create time rather than all up front. "
                "When false, all content synchronises in advance."
            ),
        },
        "automatic_sync_enabled": {
            "type": "boolean",
            "default": False,
            "description": (
                "Whether the library participates in automatic (periodic) "
                "synchronisation. The subscription stays active either way; "
                "with this false, sync happens only via an explicit "
                "``content_library.subscribed.sync``. Automatic sync also "
                "requires the global content-library automatic-sync option to "
                "be enabled."
            ),
        },
        "authentication_method": {
            "type": "string",
            "enum": ["NONE", "BASIC"],
            "default": "NONE",
            "description": (
                "How the subscribed library authenticates to the publisher. "
                "``NONE`` for a public endpoint (the VMware TKr repo); ``BASIC`` "
                "for HTTP Basic, in which case ``username`` / ``password`` are "
                "sent."
            ),
        },
        "username": {
            "type": "string",
            "description": (
                "Username for ``BASIC`` authentication "
                "(``Content.Library.SubscriptionInfo.user_name``). Ignored when "
                "``authentication_method`` is ``NONE``."
            ),
        },
        "password": {
            "type": "string",
            "description": (
                "Password for ``BASIC`` authentication "
                "(``Content.Library.SubscriptionInfo.password``). Secret: kept "
                "off every preview / broadcast / audit-hash surface. Ignored "
                "when ``authentication_method`` is ``NONE``."
            ),
        },
        "ssl_thumbprint": {
            "type": "string",
            "description": (
                "Optional SHA-1 thumbprint of the publisher's SSL certificate "
                "(``Content.Library.SubscriptionInfo.ssl_thumbprint``). When "
                "set, the certificate is pinned to this thumbprint instead of "
                "the normal chain validation."
            ),
        },
        "description": {
            "type": "string",
            "description": "Optional human-readable library description.",
        },
    },
    "required": ["name", "subscription_url", "datastore"],
    "additionalProperties": False,
    "examples": [
        {
            "name": "<library-name>",
            "subscription_url": "https://content.example.com/lib.json",
            "datastore": "<datastore-name>",
            "on_demand": True,
            "authentication_method": "NONE",
        }
    ],
}


#: ``vmware.composite.content_library.subscribed.create`` response schema.
CONTENT_LIBRARY_SUBSCRIBED_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "created",
                "datastore_not_found",
                "ambiguous_datastore",
                "create_error",
            ],
            "description": (
                "``'created'`` — the subscribed library was created and its id "
                "returned (the background sync to the publisher has begun); "
                "``'datastore_not_found'`` / ``'ambiguous_datastore'`` — the "
                "``datastore`` name matched zero / many datastores (no library "
                "created); ``'create_error'`` — the create call itself faulted, "
                "surfaced as a structured message under ``issues`` rather than a "
                "raw vendor error."
            ),
        },
        "library_id": {
            "type": ["string", "null"],
            "description": (
                "Id of the new subscribed library (``com.vmware.content.Library``). "
                "Pass it to namespace.create in ``vm_service_spec.content_libraries``. "
                "``null`` unless ``status='created'``."
            ),
        },
        "name": {"type": ["string", "null"], "description": "Echo of the library name."},
        "subscription_url": {
            "type": ["string", "null"],
            "description": "Echo of the publisher subscription URL.",
        },
        "datastore_id": {
            "type": ["string", "null"],
            "description": "Resolved datastore moid used for the storage backing.",
        },
        "on_demand": {
            "type": ["boolean", "null"],
            "description": "Echo of the effective on-demand sync mode.",
        },
        "automatic_sync_enabled": {
            "type": ["boolean", "null"],
            "description": "Echo of the effective automatic-sync setting.",
        },
        "candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Datastore moids that matched on ``ambiguous_datastore``.",
        },
        "issues": {
            "type": "array",
            "items": _HARD_FAILURE_ISSUES_PROPERTY["items"],
            "description": "Structured issue entries on a non-``created`` status.",
        },
    },
    "required": ["status", "library_id"],
}


#: ``vmware.composite.content_library.subscribed.sync`` parameter schema.
CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Subscribed-library id (a UUID) to synchronise. Supply this **or** "
                "``library_name``; ``library_id`` wins when both are present."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Library display name, resolved to an id via "
                "``POST:/content/library?action=find``. A name matching no "
                "library returns ``status='library_not_found'``, more than one "
                "``status='ambiguous_library'``. Ignored when ``library_id`` is "
                "given."
            ),
        },
    },
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [{"library_name": "<library-name>"}],
}


#: ``vmware.composite.content_library.subscribed.sync`` response schema.
CONTENT_LIBRARY_SUBSCRIBED_SYNC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "sync_triggered",
                "invalid_reference",
                "library_not_found",
                "ambiguous_library",
                "sync_error",
            ],
            "description": (
                "``'sync_triggered'`` — the forced synchronisation was accepted "
                "(asynchronous: it does not wait for content to land, and is a "
                "no-op if a sync is already in progress); ``'invalid_reference'`` "
                "— neither ``library_id`` nor ``library_name`` was supplied; "
                "``'library_not_found'`` / ``'ambiguous_library'`` — the "
                "``library_name`` lookup matched zero / many libraries; "
                "``'sync_error'`` — the sync call itself faulted."
            ),
        },
        "library_id": {
            "type": ["string", "null"],
            "description": "The resolved library id the sync targeted.",
        },
        "candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Library ids that matched on ``ambiguous_library``.",
        },
        "issues": {
            "type": "array",
            "items": _HARD_FAILURE_ISSUES_PROPERTY["items"],
            "description": "Structured issue entries on a non-``sync_triggered`` status.",
        },
    },
    "required": ["status", "library_id"],
}


#: ``vmware.composite.content_library.subscribed.status`` parameter schema.
CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Subscribed-library id (a UUID) to read. Supply this **or** "
                "``library_name``; ``library_id`` wins when both are present."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Library display name, resolved to an id via "
                "``POST:/content/library?action=find`` (see the sync op's "
                "``library_name`` for the not-found / ambiguous semantics). "
                "Ignored when ``library_id`` is given."
            ),
        },
    },
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [{"library_name": "<library-name>"}],
}


#: ``vmware.composite.content_library.subscribed.status`` response schema.
CONTENT_LIBRARY_SUBSCRIBED_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "ok",
                "invalid_reference",
                "library_not_found",
                "ambiguous_library",
                "read_error",
            ],
            "description": (
                "``'ok'`` — the library model was read; ``'invalid_reference'`` / "
                "``'library_not_found'`` / ``'ambiguous_library'`` — reference "
                "resolution failures (see the sync op); ``'read_error'`` — the "
                "GET returned a non-object payload."
            ),
        },
        "library_id": {"type": ["string", "null"], "description": "The resolved library id."},
        "name": {"type": ["string", "null"], "description": "Library display name."},
        "type": {
            "type": ["string", "null"],
            "description": "Library type (``SUBSCRIBED`` for a subscribed library).",
        },
        "subscription_url": {
            "type": ["string", "null"],
            "description": "Publisher subscription URL (no secret in the GET response).",
        },
        "authentication_method": {
            "type": ["string", "null"],
            "description": "Subscription authentication method (``NONE`` / ``BASIC``).",
        },
        "automatic_sync_enabled": {
            "type": ["boolean", "null"],
            "description": "Whether automatic synchronisation is enabled.",
        },
        "on_demand": {
            "type": ["boolean", "null"],
            "description": "Whether the library synchronises item content on demand.",
        },
        "last_sync_time": {
            "type": ["string", "null"],
            "description": (
                "ISO-8601 timestamp of the last successful synchronisation — the "
                "readiness signal: populated once the first metadata sync "
                "completes. ``null`` before the first sync."
            ),
        },
        "description": {
            "type": ["string", "null"],
            "description": "Library description, if any.",
        },
        "storage_backings": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "DATASTORE or OTHER."},
                    "datastore_id": {
                        "type": "string",
                        "description": "Datastore moid of a DATASTORE backing.",
                    },
                    "storage_uri": {
                        "type": "string",
                        "description": "URI of an OTHER backing.",
                    },
                },
            },
            "description": "The library's storage backings (as returned by vCenter).",
        },
        "candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Library ids that matched on ``ambiguous_library``.",
        },
        "issues": {
            "type": "array",
            "items": _HARD_FAILURE_ISSUES_PROPERTY["items"],
            "description": "Structured issue entries on a non-``ok`` status.",
        },
    },
    "required": ["status", "library_id"],
}


#: ``vmware.composite.content_library.subscribed.items.list`` parameter schema.
CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": (
                "Subscribed-library id (a UUID) whose items to list. Supply this **or** "
                "``library_name``; ``library_id`` wins when both are present."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Library display name, resolved to an id via "
                "``POST:/content/library?action=find`` (see the sync op for the "
                "not-found / ambiguous semantics). Ignored when ``library_id`` "
                "is given."
            ),
        },
    },
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [{"library_name": "<library-name>"}],
}


#: ``vmware.composite.content_library.subscribed.items.list`` response schema.
#:
#: The ``items`` collection is JSONFlux-reduced automatically once it crosses
#: the dispatcher's 50-row / 4 KB threshold (drill in with ``result_query``).
CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "invalid_reference",
                "library_not_found",
                "ambiguous_library",
            ],
            "description": (
                "Present only on a reference-resolution failure; a successful "
                "listing omits ``status`` and returns ``library_id`` + "
                "``item_count`` + ``items``."
            ),
        },
        "library_id": {"type": ["string", "null"], "description": "The resolved library id."},
        "item_count": {
            "type": "integer",
            "description": "Number of items read from the library.",
        },
        "items": {
            "type": "array",
            "description": (
                "One row per library item. For a TKr library each row's "
                "``name`` is a Tanzu Kubernetes release version and ``cached`` "
                "indicates whether the (multi-GB) image content is downloaded."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Library item id."},
                    "name": {
                        "type": ["string", "null"],
                        "description": "Item name (the TKr version for a TKr item).",
                    },
                    "type": {
                        "type": ["string", "null"],
                        "description": "Item type (e.g. ``ovf``, ``vm-template``).",
                    },
                    "version": {
                        "type": ["string", "null"],
                        "description": "Item metadata version.",
                    },
                    "cached": {
                        "type": ["boolean", "null"],
                        "description": "Whether the item's content is synchronised locally.",
                    },
                    "size": {
                        "type": ["integer", "null"],
                        "description": "Aggregate size of the item's files in bytes, if reported.",
                    },
                    "last_sync_time": {
                        "type": ["string", "null"],
                        "description": "ISO-8601 timestamp of the item's last sync.",
                    },
                },
                "required": ["id"],
            },
        },
        "candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Library ids that matched on ``ambiguous_library``.",
        },
        "issues": {
            "type": "array",
            "items": _HARD_FAILURE_ISSUES_PROPERTY["items"],
            "description": "Structured issue entries on a reference-resolution failure.",
        },
    },
    "required": ["library_id"],
}


# ---------------------------------------------------------------------------
# vm.resource_allocation.show / .set (#3880) -- a VM's CPU + memory limit /
# reservation (vim ``ResourceAllocationInfo``; no REST expression).
# ---------------------------------------------------------------------------

_VM_MOID_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Managed-object ID of the VM (e.g. 'vm-42').",
}

#: One ``ResourceAllocationInfo`` projection as the two ops return it.
_VM_ALLOCATION_VIEW_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "limit": {
            "type": ["integer", "null"],
            "description": "Hard cap (CPU: MHz, memory: MB); -1 = unlimited.",
        },
        "reservation": {
            "type": ["integer", "null"],
            "description": "Guaranteed amount (CPU: MHz, memory: MB).",
        },
        "shares": {
            "type": ["object", "null"],
            "properties": {
                "level": {
                    "type": ["string", "null"],
                    "description": "low / normal / high / custom.",
                },
                "shares": {"type": ["integer", "null"]},
            },
        },
    },
}

#: ``{cpu_allocation, memory_allocation}`` pair (``before`` / ``after``).
_VM_ALLOCATION_PAIR_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "cpu_allocation": _VM_ALLOCATION_VIEW_SCHEMA,
        "memory_allocation": _VM_ALLOCATION_VIEW_SCHEMA,
    },
}

#: ``vmware.composite.vm.resource_allocation.show`` parameter schema.
VM_RESOURCE_ALLOCATION_SHOW_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"vm": _VM_MOID_PARAM},
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}

#: ``vmware.composite.vm.resource_allocation.show`` response schema.
VM_RESOURCE_ALLOCATION_SHOW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ok", "vm_not_found"],
            "description": (
                "``'ok'`` -- the allocation was read; ``'vm_not_found'`` -- no "
                "readable allocation for the moid."
            ),
        },
        "vm": {"type": "string"},
        "name": {"type": ["string", "null"], "description": "VM display name."},
        "cpu_allocation": _VM_ALLOCATION_VIEW_SCHEMA,
        "memory_allocation": _VM_ALLOCATION_VIEW_SCHEMA,
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "vm"],
}

#: ``vmware.composite.vm.resource_allocation.set`` parameter schema.
#:
#: At least one allocation field is required (the vm.resize at-least-one-of
#: ``anyOf`` shape). Only the fields passed are sent; the rest stay as-is.
VM_RESOURCE_ALLOCATION_SET_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": _VM_MOID_PARAM,
        "cpu_limit_mhz": {
            "type": "integer",
            "minimum": -1,
            "not": {"const": 0},
            "description": "CPU limit in MHz (>= 1); -1 clears the limit (unlimited).",
        },
        "cpu_reservation_mhz": {
            "type": "integer",
            "minimum": 0,
            "description": "CPU reservation in MHz.",
        },
        "memory_limit_mb": {
            "type": "integer",
            "minimum": -1,
            "not": {"const": 0},
            "description": "Memory limit in MB (>= 1); -1 clears the limit (unlimited).",
        },
        "memory_reservation_mb": {
            "type": "integer",
            "minimum": 0,
            "description": "Memory reservation in MB.",
        },
    },
    "required": ["vm"],
    "anyOf": [
        {"required": ["cpu_limit_mhz"]},
        {"required": ["cpu_reservation_mhz"]},
        {"required": ["memory_limit_mb"]},
        {"required": ["memory_reservation_mb"]},
    ],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "cpu_limit_mhz": -1, "memory_reservation_mb": 2048}],
}

#: ``vmware.composite.vm.resource_allocation.set`` response schema.
VM_RESOURCE_ALLOCATION_SET_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "set",
                "unchanged",
                "invalid_request",
                "vm_not_found",
                "partial",
                "timeout",
            ],
            "description": (
                "``'set'`` -- ReconfigVM_Task succeeded (``after`` re-read); "
                "``'unchanged'`` -- the request already matched, no task; "
                "``'invalid_request'`` -- refused before any write (no field, "
                "out of range, or a limit below the reservation); "
                "``'vm_not_found'`` -- no readable allocation for the moid; "
                "``'partial'`` -- the task succeeded but the re-read ``after`` "
                "does not match every requested field; ``'timeout'`` -- the "
                "task did not finish within the poll bound. A task fault is "
                "not a status: it raises (``connector_error``)."
            ),
        },
        "vm": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "requested": {
            "type": "object",
            "properties": {
                "cpu_limit_mhz": {"type": "integer"},
                "cpu_reservation_mhz": {"type": "integer"},
                "memory_limit_mb": {"type": "integer"},
                "memory_reservation_mb": {"type": "integer"},
            },
            "description": "The allocation params that were passed (only those are sent).",
        },
        "before": _VM_ALLOCATION_PAIR_SCHEMA,
        "after": _VM_ALLOCATION_PAIR_SCHEMA,
        "task": {"type": ["string", "null"], "description": "ReconfigVM_Task moid."},
        "task_state": {
            "type": ["string", "null"],
            "description": "success / error / timeout once the task was issued.",
        },
        "guidance": {"type": ["string", "null"]},
    },
    "required": ["status", "vm"],
}


# ---------------------------------------------------------------------------
# Teardown deletes (#3339 / #3331 delete half) -- the governed inverses of the
# create-side composites. One uniform response envelope:
# ``{status, object, blockers, task, task_state, guidance}``.
# ---------------------------------------------------------------------------

#: The relative datastore-path rules shared by ``datastore.file.delete`` /
#: ``datastore.dir.create`` (preview time, here) and the handlers'
#: ``_datastore_browse.path_problem`` (dispatch time). Segments are separated
#: by ``/``; no segment may be empty, start with ``.`` (hidden / system files
#: at any depth, and so no ``.`` / ``..``), start or end with whitespace, or
#: contain wildcards, brackets, backslashes or control characters. The
#: top-level entry may not be ``contentlib-*`` (content-library backing),
#: ``fcd`` or ``catalog`` (first-class disks and their index), ignoring case.
_PATH_MID = r"[^/\\\[\]*?\x00-\x1f\x7f]"
_PATH_FIRST = r"[^./\\\[\]*?\s\x00-\x1f\x7f]"
_PATH_LAST = r"[^/\\\[\]*?\s\x00-\x1f\x7f]"
_PATH_SEGMENT = _PATH_FIRST + "(?:" + _PATH_MID + "*" + _PATH_LAST + ")?"
DATASTORE_PATH_PATTERN: str = (
    r"^(?![cC][oO][nN][tT][eE][nN][tT][lL][iI][bB]-)"
    r"(?![fF][cC][dD](?:/|$))"
    r"(?![cC][aA][tT][aA][lL][oO][gG](?:/|$))" + _PATH_SEGMENT + "(?:/" + _PATH_SEGMENT + ")*$"
)

#: Moid / id shapes shared by the teardown schemas and their handlers.
DATASTORE_MOID_PATTERN: str = r"^datastore-[0-9]+$"
PORTGROUP_MOID_PATTERN: str = r"^dvportgroup-[0-9]+$"
HOST_MOID_PATTERN: str = r"^host-[0-9]+$"
#: Cluster (``ClusterComputeResource``) moids, e.g. ``domain-c7``.
CLUSTER_MOID_PATTERN: str = r"^domain-c[0-9]+$"
#: Folder moids (``group-v`` VM, ``group-h`` host, ``group-s`` datastore,
#: ``group-n`` network, ``group-d`` datacenter folders). ``group-p`` is a
#: datastore cluster and is not accepted.
FOLDER_MOID_PATTERN: str = r"^group-[a-oq-z][0-9]+$"

_TEARDOWN_OBJECT_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": (
        "Identity of the object acted on, as read live before any write: ``kind`` plus "
        "kind-specific keys (moid / id / name / datastore path / VLAN / sizes / counts)."
    ),
}

_TEARDOWN_BLOCKERS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "object"},
    "description": (
        "What prevents the delete (capped at 50): VMs / ports / vmkernel adapters using a "
        "port group, a folder's children, VMs owning files under a datastore path, "
        "libraries subscribed to a library. Empty when nothing blocks."
    ),
}


def _teardown_response_schema(
    statuses: list[str],
    status_description: str,
    *,
    object_properties: dict[str, Any] | None = None,
    blocker_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The shared teardown response envelope with a per-op ``status`` enum.

    ``object_properties`` / ``blocker_properties`` name the fields of the
    ``object`` and of each ``blockers`` entry for one op.
    """
    obj = dict(_TEARDOWN_OBJECT_SCHEMA)
    if object_properties is not None:
        obj["properties"] = object_properties
    blockers = dict(_TEARDOWN_BLOCKERS_SCHEMA)
    if blocker_properties is not None:
        blockers["items"] = {"type": "object", "properties": blocker_properties}
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses, "description": status_description},
            "object": obj,
            "blockers": blockers,
            "task": {"type": ["string", "null"], "description": "vim Task moid, when one ran."},
            "task_state": {
                "type": ["string", "null"],
                "description": "Terminal task state (success / timeout) when a task ran.",
            },
            "guidance": {"type": ["string", "null"]},
        },
        "required": ["status", "object", "blockers"],
    }


_DELETE_STATUS_COMMON = (
    "``'deleted'`` -- the delete ran and the read-back found the object gone; "
    "``'unchanged'`` -- the object was already absent, nothing was written; "
    "``'precondition_failed'`` -- refused before any write (see ``blockers`` / "
    "``guidance``); ``'still_present'`` -- the delete returned but the object still "
    "reads back. A vSphere fault on the delete is not a status: it raises "
    "(``connector_error``, audited as failed)."
)

#: ``vmware.composite.network.portgroup.delete`` parameter schema.
NETWORK_PORTGROUP_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "portgroup": {
            "type": "string",
            "pattern": PORTGROUP_MOID_PATTERN,
            "description": (
                "Distributed portgroup moid, e.g. 'dvportgroup-42' (display names are "
                "not accepted -- resolve via vmware.composite.network.portgroup.audit)."
            ),
        },
    },
    "required": ["portgroup"],
    "additionalProperties": False,
    "examples": [{"portgroup": "dvportgroup-42"}],
}

#: ``vmware.composite.network.portgroup.delete`` response schema.
NETWORK_PORTGROUP_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "precondition_failed", "still_present", "timeout"],
    _DELETE_STATUS_COMMON + " ``'timeout'`` -- Destroy_Task outran the poll bound.",
    object_properties={
        "kind": {"type": "string", "description": "Always ``distributed_portgroup``."},
        "moid": {"type": "string", "description": "Portgroup moid."},
        "name": {"type": ["string", "null"], "description": "Portgroup name."},
        "dvs": {"type": ["string", "null"], "description": "Moid of the distributed switch."},
        "vlan": {
            "type": ["object", "null"],
            "description": "``{mode: trunk, ranges}`` or ``{mode: access, vlan_id}``.",
        },
        "num_ports": {"type": ["integer", "null"], "description": "``config.numPorts``."},
        "port_binding": {
            "type": ["string", "null"],
            "description": "``config.type``: earlyBinding, lateBinding or ephemeral.",
        },
        "uplink": {"type": "boolean", "description": "Whether it is the uplink portgroup."},
        "backing_type": {
            "type": ["string", "null"],
            "description": "``config.backingType``; ``nsx`` for an NSX segment.",
        },
        "port_check": {
            "type": "string",
            "description": "``ok``, or why the connected ports could not be read.",
        },
    },
    blocker_properties={
        "kind": {"type": "string", "description": "``vm`` or ``port``."},
        "moid": {"type": "string", "description": "VM moid (kind ``vm``)."},
        "name": {"type": ["string", "null"], "description": "VM name (kind ``vm``)."},
        "id": {"type": ["string", "null"], "description": "Port key (kind ``port``)."},
        "connectee_type": {
            "type": ["string", "null"],
            "description": "What uses the port, e.g. ``vmVnic`` or ``hostVmkVnic``.",
        },
        "connected_entity": {
            "type": ["string", "null"],
            "description": "Moid of the VM or host on the port.",
        },
        "nic": {"type": ["string", "null"], "description": "NIC key on that VM or host."},
    },
)

#: ``vmware.composite.host.standard_portgroup.delete`` parameter schema.
HOST_STANDARD_PORTGROUP_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "pattern": HOST_MOID_PATTERN,
            "description": "HostSystem moid, e.g. 'host-21' (vCenter targets).",
        },
        "portgroup_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "Exact name of the port group on the host's standard vSwitch.",
        },
    },
    "required": ["host", "portgroup_name"],
    "additionalProperties": False,
    "examples": [{"host": "host-21", "portgroup_name": "<portgroup-name>"}],
}

#: ``vmware.composite.host.standard_portgroup.delete`` response schema.
HOST_STANDARD_PORTGROUP_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "precondition_failed", "still_present"],
    _DELETE_STATUS_COMMON + " ``'invalid_request'`` -- the host moid does not exist.",
    object_properties={
        "kind": {"type": "string", "description": "Always ``host_standard_portgroup``."},
        "host": {"type": "string", "description": "Host moid."},
        "name": {"type": "string", "description": "Port group name."},
        "host_name": {"type": ["string", "null"], "description": "Host name."},
        "vswitch": {"type": ["string", "null"], "description": "Standard switch name."},
        "vlan": {
            "type": ["object", "null"],
            "description": (
                "``{mode, vlan_id}``; mode is ``untagged`` (0), ``access`` or ``trunk_all`` (4095)."
            ),
        },
        "active_ports": {"type": "integer", "description": "Ports in use on the port group."},
    },
    blocker_properties={
        "kind": {"type": "string", "description": "``vm`` or ``vmkernel_adapter``."},
        "moid": {"type": "string", "description": "VM moid (kind ``vm``)."},
        "id": {"type": ["string", "null"], "description": "Adapter, e.g. ``vmk1``."},
        "name": {"type": ["string", "null"], "description": "VM name; null for an adapter."},
    },
)

#: ``vmware.composite.folder.delete`` parameter schema.
FOLDER_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folder": {
            "type": "string",
            "pattern": FOLDER_MOID_PATTERN,
            "description": (
                "Folder moid, e.g. 'group-v1234' (as folder.create returns it). A datastore "
                "cluster ('group-p...') is not accepted."
            ),
        },
    },
    "required": ["folder"],
    "additionalProperties": False,
    "examples": [{"folder": "group-v1234"}],
}

#: ``vmware.composite.folder.delete`` response schema.
FOLDER_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "precondition_failed", "still_present", "timeout"],
    _DELETE_STATUS_COMMON + " ``'timeout'`` -- Destroy_Task outran the poll bound.",
    object_properties={
        "kind": {"type": "string", "description": "Always ``folder``."},
        "moid": {"type": "string", "description": "Folder moid."},
        "name": {"type": ["string", "null"], "description": "Folder name."},
        "parent": {"type": ["string", "null"], "description": "Parent object moid."},
        "child_type": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Object types the folder can hold (vim ``childType``).",
        },
        "child_count": {"type": "integer", "description": "Number of objects in the folder."},
    },
    blocker_properties={
        "kind": {"type": "string", "description": "vim type of the child, like VirtualMachine."},
        "moid": {"type": "string", "description": "Child moid."},
        "name": {"type": ["string", "null"], "description": "Child name."},
    },
)

#: ``vmware.composite.cluster.drs_rule.delete`` parameter schema.
CLUSTER_DRS_RULE_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "pattern": CLUSTER_MOID_PATTERN,
            "description": "Cluster moid, e.g. 'domain-c7' (the cluster the rule is on).",
        },
        "rule_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact name of the DRS rule to delete (any rule type). A name that two or "
                "more rules on the cluster share is refused."
            ),
        },
    },
    "required": ["cluster", "rule_name"],
    "additionalProperties": False,
    "examples": [{"cluster": "domain-c7", "rule_name": "<rule-name>"}],
}

#: ``vmware.composite.cluster.drs_rule.delete`` response schema.
CLUSTER_DRS_RULE_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "precondition_failed", "still_present", "timeout"],
    _DELETE_STATUS_COMMON
    + " Here ``'precondition_failed'`` means two or more rules have this name. "
    "``'invalid_request'`` -- the cluster moid does not exist. ``'timeout'`` -- "
    "ReconfigureComputeResource_Task outran the poll bound.",
    object_properties={
        "kind": {"type": "string", "description": "Always ``drs_rule``."},
        "cluster": {"type": "string", "description": "Cluster moid."},
        "cluster_name": {"type": ["string", "null"], "description": "Cluster name."},
        "name": {"type": "string", "description": "Rule name."},
        "key": {"type": ["integer", "null"], "description": "Rule key (vim ``key``)."},
        "rule_type": {
            "type": ["string", "null"],
            "description": (
                "``affinity``, ``anti_affinity`` or ``vm_host``; another vim rule type by its "
                "type name."
            ),
        },
        "enabled": {"type": ["boolean", "null"], "description": "Whether the rule is on."},
        "mandatory": {
            "type": ["boolean", "null"],
            "description": "Whether it is a 'must' rule.",
        },
        "vms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vm": {"type": "string", "description": "VM moid."},
                    "name": {"type": ["string", "null"], "description": "VM name."},
                },
            },
            "description": "VMs of a VM-to-VM rule (capped at 50).",
        },
        "vm_count": {"type": "integer", "description": "Number of VMs in the rule."},
        "vm_group": {
            "type": ["string", "null"],
            "description": "VM group of a ``vm_host`` rule.",
        },
        "affine_host_group": {
            "type": ["string", "null"],
            "description": "Host group the VMs run on (``vm_host`` rule).",
        },
        "anti_affine_host_group": {
            "type": ["string", "null"],
            "description": "Host group the VMs stay away from (``vm_host`` rule).",
        },
    },
    blocker_properties={
        "kind": {"type": "string", "description": "Always ``drs_rule``."},
        "key": {"type": ["integer", "null"], "description": "Key of a rule with this name."},
        "rule_type": {"type": ["string", "null"], "description": "Type of that rule."},
    },
)

_DATASTORE_MOID_PARAM: dict[str, Any] = {
    "type": "string",
    "pattern": DATASTORE_MOID_PATTERN,
    "description": ("Datastore moid, e.g. 'datastore-17' (see vmware.composite.datastore.usage)."),
}

_DATASTORE_RELATIVE_PATH_PARAM: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 1024,
    "pattern": DATASTORE_PATH_PATTERN,
    "description": (
        "Path relative to the datastore root, e.g. 'old-appliance' or 'iso/stale.iso'. "
        "Not the root and no leading '/'. No segment may be empty, start with '.', or start "
        "or end with a space; no wildcards, brackets or backslashes. The top-level folder "
        "may not be 'contentlib-*', 'fcd' or 'catalog'."
    ),
}

#: ``vmware.composite.datastore.file.delete`` parameter schema.
DATASTORE_FILE_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "datastore": _DATASTORE_MOID_PARAM,
        "path": _DATASTORE_RELATIVE_PATH_PARAM,
    },
    "required": ["datastore", "path"],
    "additionalProperties": False,
    "examples": [{"datastore": "datastore-17", "path": "<folder-name>"}],
}

#: ``vmware.composite.datastore.file.delete`` response schema.
DATASTORE_FILE_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "precondition_failed", "still_present", "timeout"],
    _DELETE_STATUS_COMMON
    + " ``'invalid_request'`` -- the path or datastore reference is unacceptable; "
    "``'timeout'`` -- DeleteDatastoreFile_Task outran the poll bound.",
    object_properties={
        "kind": {
            "type": "string",
            "description": (
                "``datastore_file`` or ``datastore_directory``; ``datastore_path`` when "
                "the path was not found."
            ),
        },
        "datastore": {"type": ["string", "null"], "description": "Datastore moid."},
        "path": {"type": ["string", "null"], "description": "The path from the call."},
        "datastore_name": {"type": "string", "description": "Datastore name."},
        "datastore_type": {"type": "string", "description": "Datastore type, e.g. ``NFS``."},
        "datastore_path": {
            "type": "string",
            "description": "The full datastore path, ``[<datastore>] <path>``.",
        },
        "resolved_path": {
            "type": "string",
            "description": "The datastore path as vSphere found it; this is what is deleted.",
        },
        "file_type": {
            "type": ["string", "null"],
            "description": "vSphere ``FileInfo`` type, e.g. ``FolderFileInfo``.",
        },
        "size_bytes": {"type": ["integer", "null"], "description": "``FileInfo.fileSize``."},
        "modified": {
            "type": ["string", "null"],
            "description": "``FileInfo.modification`` (date and time).",
        },
    },
    blocker_properties={
        "kind": {"type": "string", "description": "``vm``."},
        "moid": {"type": "string", "description": "VM moid."},
        "name": {"type": ["string", "null"], "description": "VM name."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files of that VM under the path (at most 10).",
        },
    },
)

#: ``vmware.composite.datastore.dir.create`` parameter schema.
DATASTORE_DIR_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "datastore": _DATASTORE_MOID_PARAM,
        "path": _DATASTORE_RELATIVE_PATH_PARAM,
        "create_parents": {
            "type": "boolean",
            "default": False,
            "description": "Create missing intermediate directories (mkdir -p).",
        },
    },
    "required": ["datastore", "path"],
    "additionalProperties": False,
    "examples": [{"datastore": "datastore-17", "path": "<folder-name>", "create_parents": False}],
}

#: ``vmware.composite.datastore.dir.create`` response schema.
DATASTORE_DIR_CREATE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["created", "unchanged", "invalid_request", "precondition_failed", "not_verified"],
    (
        "``'created'`` -- MakeDirectory ran and the directory reads back; ``'unchanged'`` "
        "-- it already existed; ``'invalid_request'`` -- the path or datastore reference "
        "is unacceptable; ``'precondition_failed'`` -- a file holds the name, or the "
        "parent is missing without create_parents; ``'not_verified'`` -- MakeDirectory "
        "returned but the directory does not read back. A vSphere fault raises."
    ),
    object_properties={
        "kind": {"type": "string", "description": "Always ``datastore_directory``."},
        "datastore": {"type": ["string", "null"], "description": "Datastore moid."},
        "path": {"type": ["string", "null"], "description": "The path from the call."},
        "datastore_name": {"type": "string", "description": "Datastore name."},
        "datastore_type": {"type": "string", "description": "Datastore type, e.g. ``NFS``."},
        "datastore_path": {
            "type": "string",
            "description": "The full datastore path, ``[<datastore>] <path>``.",
        },
    },
)

#: ``vmware.composite.content_library.delete`` parameter schema.
CONTENT_LIBRARY_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": "Library id (a UUID).",
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": "Library name (a name that matches more than one library is refused).",
        },
        "delete_items": {
            "type": "boolean",
            "default": False,
            "description": "Required (true) to delete a library that still holds items.",
        },
    },
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [{"library_name": "<library-name>", "delete_items": True}],
}

#: ``vmware.composite.content_library.delete`` response schema.
CONTENT_LIBRARY_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "precondition_failed", "still_present"],
    _DELETE_STATUS_COMMON + " ``'invalid_request'`` -- an ambiguous library_name.",
    object_properties={
        "kind": {"type": "string", "description": "Always ``content_library``."},
        "id": {"type": ["string", "null"], "description": "Library id (a UUID)."},
        "name": {"type": ["string", "null"], "description": "Library name."},
        "type": {"type": ["string", "null"], "description": "LOCAL or SUBSCRIBED."},
        "published": {
            "type": "boolean",
            "description": "Whether the library is published to subscribers.",
        },
        "datastores": {
            "type": "array",
            "items": {"type": ["string", "null"]},
            "description": "Datastore moids that back the library.",
        },
        "item_count": {"type": "integer", "description": "Number of items in the library."},
        "total_size_bytes": {
            "type": "integer",
            "description": "Size of the items in bytes (only in the approval preview).",
        },
    },
    blocker_properties={
        "kind": {
            "type": "string",
            "description": "``subscribed_library`` (a library that subscribes to it) or ``vm``.",
        },
        "id": {"type": ["string", "null"], "description": "Id of the subscribed library."},
        "name": {"type": ["string", "null"], "description": "Library or VM name."},
        "vcenter": {
            "type": ["string", "null"],
            "description": "vCenter of a subscribed library on another vCenter.",
        },
        "moid": {"type": "string", "description": "VM moid of a ``vm`` blocker."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Library files the VM has in a CD-ROM or floppy drive.",
        },
    },
)

#: ``vmware.composite.content_library.item.delete`` parameter schema.
CONTENT_LIBRARY_ITEM_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": "Library item id (a UUID).",
        },
        "item_name": {
            "type": "string",
            "minLength": 1,
            "description": "Item name; needs library_id or library_name.",
        },
        "library_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": "Id (a UUID) of the library that holds item_name.",
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the library that holds item_name.",
        },
    },
    "anyOf": [
        {"required": ["item_id"]},
        {"required": ["item_name", "library_id"]},
        {"required": ["item_name", "library_name"]},
    ],
    "additionalProperties": False,
    "examples": [{"item_name": "<item-name>", "library_name": "<library-name>"}],
}

#: ``vmware.composite.content_library.item.delete`` response schema.
CONTENT_LIBRARY_ITEM_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "precondition_failed", "still_present"],
    _DELETE_STATUS_COMMON + " ``'invalid_request'`` -- an ambiguous item or library name.",
    object_properties={
        "kind": {"type": "string", "description": "Always ``content_library_item``."},
        "id": {"type": ["string", "null"], "description": "Item id (a UUID)."},
        "name": {"type": ["string", "null"], "description": "Item name."},
        "type": {"type": ["string", "null"], "description": "Item type, e.g. ``ovf`` or ``iso``."},
        "size_bytes": {"type": ["integer", "null"], "description": "Item size in bytes."},
        "library_id": {"type": ["string", "null"], "description": "Id of the item's library."},
        "library_name": {"type": ["string", "null"], "description": "Name of the library."},
        "library_type": {
            "type": ["string", "null"],
            "description": "LOCAL or SUBSCRIBED; only LOCAL items can be deleted.",
        },
    },
    blocker_properties={
        "kind": {"type": "string", "description": "Always ``vm``."},
        "moid": {"type": "string", "description": "VM moid."},
        "name": {"type": ["string", "null"], "description": "VM name."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Item files the VM has in a CD-ROM or floppy drive.",
        },
    },
)


#: ``vmware.composite.vm.snapshot.create`` parameter schema.
VM_SNAPSHOT_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "pattern": VM_MOID_PATTERN,
            "description": (
                "Moid of the VM, e.g. 'vm-42'. Not the VM name: vmware.vm.info finds the "
                "moid from the name."
            ),
        },
        "snapshot_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the new snapshot. When the VM already has a snapshot with this "
                "name, nothing is created (status 'unchanged')."
            ),
        },
        "description": {
            "type": "string",
            "description": "Optional text stored with the snapshot, for example why it was taken.",
        },
        "memory": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also save the memory of the running VM, so a revert brings it back running. "
                "Slower and uses more disk space. Default false: disks only, and a revert "
                "leaves the VM powered off."
            ),
        },
        "quiesce": {
            "type": "boolean",
            "default": False,
            "description": (
                "Ask VMware Tools in the guest to flush its file systems first, for a "
                "consistent disk state. vCenter ignores it when the VM is off or has no "
                "VMware Tools. Default false."
            ),
        },
    },
    "required": ["vm", "snapshot_name"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "snapshot_name": "<snapshot-name>", "description": "<reason>"}],
}

#: ``vmware.composite.vm.snapshot.create`` response schema.
VM_SNAPSHOT_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": _HARD_FAILURE_ISSUES_PROPERTY,
        "status": {
            "type": "string",
            "enum": ["created", "unchanged", "not_found", "invalid_request", "timeout"],
            "description": (
                "``'created'`` -- the task ended and the snapshot was taken; "
                "``'unchanged'`` -- the VM already has a snapshot with this name, nothing "
                "was written; ``'not_found'`` -- no VM with this moid; "
                "``'invalid_request'`` -- the VM moid has the wrong shape; ``'timeout'`` -- "
                "CreateSnapshotEx_Task outran the poll bound, it may still finish. A vSphere "
                "fault is not a status: it raises (``connector_error``, audited as failed)."
            ),
        },
        "vm": {"type": "string", "description": "VM moid."},
        "vm_name": {"type": ["string", "null"], "description": "VM name."},
        "snapshot_id": {
            "type": ["string", "null"],
            "description": (
                "Moid of the new snapshot (created), or of the snapshot that already has this "
                "name (unchanged)."
            ),
        },
        "snapshot": {
            **SNAPSHOT_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": (
                "The snapshot as vmware.snapshot.list shows it, read again after the task; "
                "null when it does not read back."
            ),
        },
        "task": {"type": ["string", "null"], "description": "vCenter task moid, when one ran."},
        "task_state": {
            "type": ["string", "null"],
            "description": "Final task state (success or timeout), when a task ran.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "What to do next, when the snapshot was not created as asked.",
        },
    },
    "required": ["status", "vm"],
}

#: ``vmware.composite.vm.snapshot.delete`` parameter schema.
VM_SNAPSHOT_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "pattern": VM_MOID_PATTERN,
            "description": "Moid of the VM the snapshot belongs to, e.g. 'vm-42'.",
        },
        "snapshot_id": {
            "type": "string",
            "pattern": VM_SNAPSHOT_MOID_PATTERN,
            "description": (
                "Moid of the snapshot to delete, e.g. 'snapshot-12' (from "
                "vmware.snapshot.list). It must be a snapshot of this VM."
            ),
        },
        "remove_children": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also delete every snapshot below this one. Default false: the child "
                "snapshots stay, and only this restore point goes."
            ),
        },
    },
    "required": ["vm", "snapshot_id"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "snapshot_id": "snapshot-12"}],
}

#: ``vmware.composite.vm.snapshot.delete`` response schema.
VM_SNAPSHOT_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "still_present", "timeout"],
    "``'deleted'`` -- the delete ran and the snapshot no longer reads back; "
    "``'unchanged'`` -- the VM has no snapshot with this id, nothing was written; "
    "``'invalid_request'`` -- no VM with this moid, or a moid has the wrong shape; "
    "``'still_present'`` -- the task ended but the snapshot still reads back; "
    "``'timeout'`` -- RemoveSnapshot_Task outran the poll bound, it may still finish. A "
    "vSphere fault on the delete is not a status: it raises (``connector_error``, audited "
    "as failed).",
    object_properties={
        "kind": {"type": "string", "description": "Always ``vm_snapshot``."},
        "vm": {"type": "string", "description": "VM moid."},
        "vm_name": {"type": ["string", "null"], "description": "VM name."},
        "snapshot_id": {"type": "string", "description": "Snapshot moid."},
        "remove_children": {
            "type": "boolean",
            "description": "Whether the snapshots below this one are deleted too.",
        },
        "name": {"type": ["string", "null"], "description": "Snapshot name."},
        "description": {"type": ["string", "null"], "description": "Snapshot description."},
        "created": {"type": ["string", "null"], "description": "When the snapshot was taken."},
        "power_state": {
            "type": ["string", "null"],
            "description": "The VM power state saved in the snapshot.",
        },
        "current": {
            "type": "boolean",
            "description": "Whether it is vCenter's current snapshot of the VM.",
        },
        "child_count": {
            "type": "integer",
            "description": "Number of snapshots below this one (children and their children).",
        },
    },
)
