# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema 2020-12 parameter + response schemas for the 27 vmware-rest composites.

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
* The 5 read composites are read-only -- the registration call site
  pins ``safety_level="safe"`` and ``requires_approval=False`` on
  each. The 18 write composites inherit T4's
  ``safety_level="dangerous"`` + ``requires_approval=True`` defaults
  (G3.1-T6 / #509, single-VM ``vm.power`` / #2301, the mutating
  VI-JSON ``vm.disk.grow`` / #2893, the folder-template
  ``vm.clone_from_template`` / #2894, the vim cluster / inventory
  writes ``cluster.drs_rule.create`` + ``folder.create`` / #2895,
  the #2891 hardware writes ``vm.resize`` / ``vm.nic.repoint`` /
  ``vm.device.cdrom``, and the two GOSC composites
  ``guest.customization_spec.create`` / ``vm.customize`` / #2892). The schema
  text reflects which side of that line
  each composite sits on; the registration call site enforces the
  policy.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA",
    "CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA",
    "CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA",
    "CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA",
    "CLUSTER_PATCH_PARAMETER_SCHEMA",
    "CLUSTER_PATCH_RESPONSE_SCHEMA",
    "DATASTORE_USAGE_MAX_VM_NAMES",
    "DATASTORE_USAGE_PARAMETER_SCHEMA",
    "DATASTORE_USAGE_RESPONSE_SCHEMA",
    "EVENT_TAIL_PARAMETER_SCHEMA",
    "EVENT_TAIL_RESPONSE_SCHEMA",
    "FOLDER_CREATE_PARAMETER_SCHEMA",
    "FOLDER_CREATE_RESPONSE_SCHEMA",
    "GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA",
    "GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA",
    "HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA",
    "HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA",
    "HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA",
    "HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA",
    "HOST_DISK_MARK_FLASH_PARAMETER_SCHEMA",
    "HOST_DISK_MARK_FLASH_RESPONSE_SCHEMA",
    "HOST_EVACUATE_PARAMETER_SCHEMA",
    "HOST_EVACUATE_RESPONSE_SCHEMA",
    "HOST_SERVICE_CONTROL_PARAMETER_SCHEMA",
    "HOST_SERVICE_CONTROL_RESPONSE_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA",
    "PERFORMANCE_SUMMARY_PARAMETER_SCHEMA",
    "PERFORMANCE_SUMMARY_RESPONSE_SCHEMA",
    "VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA",
    "VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA",
    "VM_CLONE_PARAMETER_SCHEMA",
    "VM_CLONE_RESPONSE_SCHEMA",
    "VM_CREATE_PARAMETER_SCHEMA",
    "VM_CREATE_RESPONSE_SCHEMA",
    "VM_CUSTOMIZE_PARAMETER_SCHEMA",
    "VM_CUSTOMIZE_RESPONSE_SCHEMA",
    "VM_DEVICE_CDROM_PARAMETER_SCHEMA",
    "VM_DEVICE_CDROM_RESPONSE_SCHEMA",
    "VM_DISK_GROW_PARAMETER_SCHEMA",
    "VM_DISK_GROW_RESPONSE_SCHEMA",
    "VM_MIGRATE_PARAMETER_SCHEMA",
    "VM_MIGRATE_RESPONSE_SCHEMA",
    "VM_NIC_REPOINT_PARAMETER_SCHEMA",
    "VM_NIC_REPOINT_RESPONSE_SCHEMA",
    "VM_POWER_BULK_PARAMETER_SCHEMA",
    "VM_POWER_BULK_RESPONSE_SCHEMA",
    "VM_POWER_PARAMETER_SCHEMA",
    "VM_POWER_RESPONSE_SCHEMA",
    "VM_RESIZE_PARAMETER_SCHEMA",
    "VM_RESIZE_RESPONSE_SCHEMA",
    "VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA",
    "VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA",
]


#: ``vmware.composite.cluster.drs_recommendations`` parameter schema.
#:
#: Reads cluster summary + DRS state (optionally surfacing the cluster's
#: current DRS recommendation list). The composite dispatches one
#: ``GET:/vcenter/cluster/{cluster}`` plus one vi-json
#: ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx`` (reading
#: ``ClusterComputeResource.configurationEx.drsConfig`` and, on request,
#: ``drsRecommendation``) to a single target -- the pinned vcenter.yaml
#: serves no cluster DRS REST resource (#2986).
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
}


#: ``vmware.composite.event.tail`` parameter schema.
#:
#: Reads recent events from EventManager via the vi-json
#: ``POST:/EventManager/{moId}/QueryEvents`` sub-op. Equivalent of
#: ``govc events`` against a vSphere target. The default ``moId`` is
#: the canonical ``EventManager`` singleton.
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
        "max_events": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 100,
            "description": (
                "Cap on the number of events returned. The vi-json "
                "QueryEvents call accepts a filter -- the handler "
                "applies the cap client-side after the sub-op returns "
                "so older events are dropped uniformly."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
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
                "(e.g. 'vm-1234', 'host-5678'). Required: every QueryPerf "
                "call is per-entity."
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
                "Sample interval for the QueryPerf call. The default 20 s "
                "matches vSphere's real-time historical interval."
            ),
        },
        "max_samples": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 60,
            "description": (
                "Cap on the number of returned samples per counter. "
                "Applied client-side after the sub-op returns."
            ),
        },
    },
    "required": ["entity_moid"],
    "additionalProperties": False,
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
}


#: ``vmware.composite.network.portgroup.audit`` parameter schema.
#:
#: Lists distributed portgroups with host membership + connected-VM
#: aggregation. All sub-ops are vCenter REST.
NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter_dvs": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Accepted but inert (#2970 degradation): this only ever "
                "scoped the distributed-switch listing that fed the "
                "``dvs_name`` enrichment, and the pinned vcenter.yaml "
                "serves no DVS list resource, so that step was dropped. "
                "The generic network resource has no per-DVS filter "
                "either, so the returned portgroup set was never "
                "narrowed by this value."
            ),
        },
        "include_disconnected_vms": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, the VM aggregation includes VMs whose power "
                "state is OFF or whose NIC is disconnected. Default "
                "false returns only actively-connected VMs."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
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
            "description": (
                "Cluster summary payload from "
                "``GET:/vcenter/cluster/{cluster}`` (vSphere REST owns "
                "the inner shape)."
            ),
        },
        "drs": {
            "type": "object",
            "description": (
                "DRS configuration payload: the cluster's vim "
                "``configurationEx.drsConfig`` (``ClusterDrsConfigInfo`` "
                "-- the vim API owns the inner shape) read via "
                "``RetrievePropertiesEx``; ``{}`` when the property is "
                "unset on the target."
            ),
        },
        "recommendations_history": {
            "type": "array",
            "items": {"type": "object"},
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
            "items": {"type": "object"},
            "description": (
                "Capped list of event dicts from "
                "``POST:/EventManager/{moId}/QueryEvents`` (vi-json). "
                "Truncated client-side to ``max_events``."
            ),
        },
        "count": {
            "type": "integer",
            "minimum": 0,
            "description": "Post-cap length of ``events`` -- detects truncation.",
        },
        "moId": {
            "type": "string",
            "description": "EventManager managed-object ID the call targeted.",
        },
        "max_events_applied": {
            "type": "integer",
            "minimum": 1,
            "description": "Effective ``max_events`` cap applied to the response.",
        },
    },
    "required": ["events", "count", "moId", "max_events_applied"],
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
            "items": {"type": "object"},
            "description": (
                "Counters returned by ``QueryAvailablePerfMetric`` for the entity (vi-json)."
            ),
        },
        "samples": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Capped sample list from ``QueryPerf`` (vi-json). "
                "Truncated client-side to ``max_samples``."
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
                            "per-datastore filter was not applied (#2975)."
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
                        "description": "Parent DVS managed-object ID, if present.",
                    },
                    "dvs_name": {
                        "type": ["string", "null"],
                        "description": (
                            "Always ``null`` (#2970 degradation): the "
                            "DVS listing that resolved display names is "
                            "not served by the pinned spec. Key retained "
                            "for response-envelope stability."
                        ),
                    },
                    "type": {
                        "type": ["string", "null"],
                        "description": "Portgroup type from the listing row.",
                    },
                    "vm_count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Number of VMs attached to this portgroup.",
                    },
                    "vm_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of VMs attached to this portgroup.",
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
                "via ``filter.datacenters`` (#3115); a residual "
                "ambiguity fails with a structured ``rolled_back`` "
                "carrying ``candidate_folders`` — pass ``folder`` to "
                "disambiguate. Ignored when ``folder`` is supplied."
            ),
        },
        "folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional VM folder moid pin (#3115), mirroring the "
                "#3096 placement-pin pattern. When present the "
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
                "Optional ResourcePool moid pin (#3096). Threaded into "
                "the CreateSpec ``placement`` alongside the resolved "
                "folder moid. The pinned spec marks ``resource_pool`` as "
                "currently required when neither host nor cluster is "
                "given, so multi-host clusters should pin it; absent, "
                "vCenter's placement defaulting applies. On a pre-9.0 "
                "target it is REQUIRED (#3099): vim CreateVM_Task takes "
                "an explicit pool and has no placement defaulting."
            ),
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional Datastore moid pin (#3096) for the VM home and "
                "disk storage. Threaded into the CreateSpec "
                "``placement``; absent, vCenter's placement defaulting "
                "applies — on hosts with host-local datastores the "
                "default may land the VM on the wrong store. On a "
                "pre-9.0 target it is REQUIRED (#3099): the vim arm "
                "resolves its display name into the "
                "``files.vmPathName`` VM home."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional HostSystem moid pin (#3096). Threaded into the "
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
                "pre-9.0 target (#3099) it is mapped to the vim guestId "
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
                "NICs. On a pre-9.0 target (#3099) NICs fold into the "
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
                },
                "required": ["capacity_gb"],
                "additionalProperties": False,
            },
            "default": [],
            "description": (
                "Optional data disks to create with the VM (#3117). On the "
                "REST arm each entry threads into the CreateSpec ``disks`` "
                "as a SCSI ``new_vmdk`` sized in bytes (vCenter fabricates "
                "the controller). On the pre-9.0 vim arm (#3099) — where "
                "``CreateVM_Task`` builds the VM verbatim and adds no "
                "controller — the arm ALWAYS folds a "
                "``VirtualLsiLogicSASController`` (so a fresh VM has a "
                "controller for the governed disk-add "
                "``POST:/vcenter/vm/{vm}/hardware/disk`` even when this "
                "list is empty) plus one ``VirtualDisk`` "
                "``fileOperation: create`` per entry, bound to it. "
                "Thin-vs-thick follows the datastore default. Empty list "
                "keeps the REST create body byte-identical to a pre-#3117 "
                "call."
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
            "minLength": 1,
            "description": (
                "Content-library template item id. Rides the deploy path as "
                "``POST:/vcenter/vm-template/library-items/"
                "{templateLibraryItem}?action=deploy``."
            ),
        },
    },
    "required": ["source_vm", "target_name", "library_item"],
    "additionalProperties": False,
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
}


#: ``vmware.composite.vm.snapshot.revert`` parameter schema.
#:
#: Idempotent revert by snapshot name. Ambiguity (multiple snapshots
#: share the name) returns ``status='ambiguous'`` rather than guessing.
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
                "Display name of the snapshot to revert to. Multiple "
                "snapshots with the same name return "
                "``status='ambiguous'`` so the caller can pick by id."
            ),
        },
    },
    "required": ["vm", "snapshot_name"],
    "additionalProperties": False,
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
}


#: ``vmware.composite.vm.power.bulk`` parameter schema.
#:
#: Resolve filter -> per-VM power action. Partial-failure tolerated
#: by default; ``fail_fast=True`` aborts on the first failure.
VM_POWER_BULK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "object",
            "description": (
                "Free-form filter dict forwarded to "
                "``GET:/vcenter/vm`` as ``filter.*`` query params. The "
                "handler does not introspect the keys; vSphere REST "
                "validates them server-side."
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
}


#: ``vmware.composite.vm.power`` parameter schema.
#:
#: Single-VM power verb. Hard verbs (``on`` / ``off`` / ``reset``) hit
#: ``POST:/vcenter/vm/{vm}/power``; soft verbs (``guest_shutdown`` /
#: ``guest_reboot``) hit ``POST:/vcenter/vm/{vm}/guest/power`` and require
#: running VMware Tools.
VM_POWER_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": "VM moid to act on.",
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
}


#: ``vmware.composite.host.evacuate`` parameter schema.
#:
#: Lists VMs on host, recursively dispatches ``vmware.composite.vm.migrate``
#: per VM, then enters maintenance. ``tolerate_partial_failure=True``
#: allows maintenance-enter with VMs left on host.
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
                "When true, enter maintenance even if some VMs failed "
                "to migrate (those VMs stay on the host). Default "
                "false aborts before maintenance-enter on any failure."
            ),
        },
    },
    "required": ["host"],
    "additionalProperties": False,
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
            "type": "string",
            "minLength": 1,
            "description": (
                "Virtual disk identifier — the REST "
                "``com.vmware.vcenter.vm.hardware.Disk`` id, which is the "
                "string form of the vim ``VirtualDevice.key`` (e.g. '2000'). "
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
}


#: ``vmware.composite.folder.create`` parameter schema.
#:
#: Create a VM folder under a named parent via the **synchronous** vim
#: ``Folder.CreateFolder`` (``/vcenter/folder`` is GET-only, so vim is the
#: sole write path). The parent is resolved by display name among the
#: ``VIRTUAL_MACHINE`` folders; no match → ``status='parent_not_found'``,
#: more than one → ``status='ambiguous_parent'``.
FOLDER_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "parent_folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the parent VM folder the new folder is created "
                "under. Resolved to its moid via ``GET:/vcenter/folder`` filtered "
                "to VIRTUAL_MACHINE folders; an ambiguous or unknown name is "
                "refused with a structured status before any write."
            ),
        },
        "folder_name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the new sub-folder to create under the parent.",
        },
    },
    "required": ["parent_folder", "folder_name"],
    "additionalProperties": False,
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
        "status": {
            "type": "string",
            "enum": ["created", "rolled_back"],
            "description": (
                "``'created'`` after every step succeeded; "
                "``'rolled_back'`` when a post-create step failed and "
                "the handler issued ``DELETE:/vcenter/vm/{vm}``."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": (
                "Newly-created VM moid. ``null`` on rollback (the VM no longer exists)."
            ),
        },
        "steps_succeeded": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Per-step success ledger: ``folder_lookup``, "
                "``create``, ``disk_attach``, ``nic_attach``, "
                "``nested_hv``, ``power_on``. ``disk_attach`` (#3117) is "
                "present when the request carried ``disks`` (folded into "
                "the create on both arms). ``folder_lookup`` is omitted "
                "when the request pinned the folder moid via the "
                "``folder`` param (#3115 — no lookup ran)."
            ),
        },
        "failed_step": {
            "type": ["string", "null"],
            "description": (
                "Name of the first failing step on rollback; ``null`` "
                "when ``status='created'``. Both arms fail closed on an "
                "invalid ``disks`` entry with ``disk_spec`` (#3117). The "
                "pre-9.0 vim arm (#3099) adds fail-closed resolution "
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
        "nested_hv": {
            "type": "boolean",
            "description": (
                "Applied VHV state (#3093). Present only when the request "
                "carried the ``nested_hv`` param — a param-absent call "
                "keeps the pre-#3093 envelope byte-identical. ``true`` "
                "iff the ``ReconfigVM_Task`` leg completed."
            ),
        },
        "candidate_folders": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Folder moids an ambiguous ``folder_name`` matched "
                "(#3115). Present only on a ``folder_lookup`` rollback "
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
                "response-envelope stability (#2970)."
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
            "items": {"type": "object"},
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
        "status": {
            "type": "string",
            "enum": ["evacuated", "partial", "aborted"],
            "description": (
                "``'evacuated'`` -- every VM migrated + host in "
                "maintenance; ``'partial'`` -- some VMs left behind "
                "(``tolerate_partial_failure=True``); ``'aborted'`` "
                "-- migration failure stopped the loop before "
                "maintenance-enter."
            ),
        },
        "host": {
            "type": "string",
            "description": "Host moid the operator targeted.",
        },
        "migrated_vms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "VM moids that migrated successfully.",
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
            "description": "VM moids whose migration failed, with reason.",
        },
        "maintenance_entered": {
            "type": "boolean",
            "description": (
                "Whether the host entered maintenance mode -- true on "
                "``evacuated``/``partial``, false on ``aborted``."
            ),
        },
    },
    "required": [
        "status",
        "host",
        "migrated_vms",
        "failed_vms",
        "maintenance_entered",
    ],
}


#: ``vmware.composite.host.detach_from_vds`` response schema.
HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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


#: ``vmware.composite.cluster.drs_rule.create`` response schema.
CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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
                "VM folder; ``'ambiguous_parent'`` — it matched more than one "
                "(both refused before any write)."
            ),
        },
        "parent_folder": {
            "type": "string",
            "description": "The parent folder display name the operator supplied.",
        },
        "parent_folder_id": {
            "type": ["string", "null"],
            "description": "Resolved parent folder moid; ``null`` on a resolution refusal.",
        },
        "new_folder_name": {"type": "string", "description": "The requested new folder name."},
        "folder": {
            "type": ["string", "null"],
            "description": (
                "The new folder's moid — CreateFolder is synchronous and returns "
                "the MoRef directly (no task poll). ``null`` on a resolution refusal."
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
}


#: ``vmware.composite.vm.nic.repoint`` parameter schema.
#:
#: Repoints an existing vNIC to a different distributed portgroup,
#: resolved by display name via
#: ``GET:/vcenter/network?filter.types=DISTRIBUTED_PORTGROUP`` (there is
#: no dedicated portgroup list resource -- the #1602 reconciliation
#: lesson). Ambiguous / missing names refuse the repoint.
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
                "Display name of the target distributed portgroup. "
                "Resolved to its network moid via "
                "``GET:/vcenter/network?filter.types=DISTRIBUTED_PORTGROUP``. "
                "A name matching zero portgroups returns "
                "``status='not_found'``; more than one returns "
                "``status='ambiguous'`` with the candidates listed."
            ),
        },
    },
    "required": ["vm", "nic", "portgroup_name"],
    "additionalProperties": False,
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
}


#: ``vmware.composite.vm.resize`` response schema.
VM_RESIZE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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
            "enum": ["repointed", "not_found", "ambiguous"],
            "description": (
                "``'repointed'`` -- the NIC backing was PATCHed to the "
                "target portgroup; ``'not_found'`` -- no distributed "
                "portgroup matched ``portgroup_name``; ``'ambiguous'`` -- "
                "more than one did (candidates listed, no PATCH issued)."
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
            "description": "The NIC's backing before the repoint (type, network, network_name).",
        },
        "requested_backing": {
            "type": "object",
            "properties": {
                "portgroup_id": {"type": ["string", "null"]},
                "portgroup_name": {"type": "string"},
            },
            "required": ["portgroup_id", "portgroup_name"],
            "description": "The target distributed portgroup (moid resolved from the name).",
        },
        "candidates": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Matching portgroup rows when ``status='ambiguous'`` (empty otherwise).",
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
            "enum": ["removed", "updated", "disconnected", "invalid_request"],
            "description": (
                "``'removed'`` -- the device was deleted; ``'updated'`` -- "
                "its backing was PATCHed; ``'disconnected'`` -- the device "
                "was disconnected in-guest; ``'invalid_request'`` -- "
                "``action='update'`` without a ``backing`` object (no write "
                "issued)."
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
            "description": "Echoed target backing when ``action='update'``; ``null`` otherwise.",
        },
        "guidance": {
            "type": ["string", "null"],
            "description": "Hint on ``invalid_request``; ``null`` otherwise.",
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
                "Selects the guest OS branch: ``linux`` builds "
                "``configuration_spec.linux_config``; ``windows`` builds "
                "``configuration_spec.windows_config`` with a sysprep body."
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
            "description": "DNS domain for the guest (``linux_config.domain``).",
        },
        "time_zone": {
            "type": "string",
            "description": (
                "Guest time zone (a tz-database name on Linux, e.g. "
                "``Europe/Vienna``). Omitted from the body when absent."
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
                        "description": "Subnet prefix length for the static IPv4 address.",
                    },
                    "gateways": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Default gateway IPs for this NIC.",
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
                "SECRET. Local Administrator password for the Windows "
                "guest (``windows_config.sysprep.gui_unattended.password``). "
                "Never serialized to any reviewer / preview / broadcast / "
                "audit surface (#1503)."
            ),
        },
        "windows_product_key": {
            "type": "string",
            "description": (
                "SECRET. Windows product / license key "
                "(``windows_config.sysprep.user_data.product_key``). Never "
                "serialized to any reviewer-facing surface (#1503)."
            ),
        },
        "windows_organization": {
            "type": "string",
            "description": "Windows registered organization (``user_data.organization``).",
        },
        "windows_full_name": {
            "type": "string",
            "description": "Windows registered owner name (``user_data.full_name``).",
        },
        "windows_join_domain": {
            "type": "string",
            "description": (
                "Active Directory domain the Windows guest joins "
                "(``sysprep.domain.domain`` with ``sysprep.domain.type = DOMAIN``)."
            ),
        },
        "windows_domain_admin_username": {
            "type": "string",
            "description": (
                "Domain account used for the join (``sysprep.domain.domain_username``)."
            ),
        },
        "windows_domain_admin_password": {
            "type": "string",
            "description": (
                "SECRET. Password for the domain-join account "
                "(``sysprep.domain.domain_password``). Never serialized to any "
                "reviewer-facing surface (#1503)."
            ),
        },
        "windows_auto_logon": {
            "type": "boolean",
            "default": False,
            "description": (
                "Whether the Windows guest auto-logs-on after customization. "
                "Also drives the required ``gui_unattended.auto_logon_count`` "
                "(``1`` when true, else ``0``)."
            ),
        },
        "windows_time_zone": {
            "type": "integer",
            "default": 85,
            "description": (
                "Windows guest time zone as a Microsoft time-zone index "
                "(``gui_unattended.time_zone``; a REQUIRED integer, distinct "
                "from the Linux ``time_zone`` tz-name string). Defaults to "
                "``85`` (GMT). See https://support.microsoft.com/help/973627."
            ),
        },
    },
    "required": ["spec_name", "os_type", "hostname"],
    "additionalProperties": False,
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
            "items": {"type": "object"},
            "description": "Identity projections of the matched VMs when ``status='ambiguous'``.",
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
            "minLength": 1,
            "description": (
                "Content-library OVF/OVA item id (passthrough). Rides the deploy "
                "path as ``POST:/vcenter/ovf/library-item/{ovfLibraryItemId}"
                "?action=deploy``. Supply this **or** ``library_item_name`` — not "
                "both needed; ``library_item`` wins when both are present."
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
                "fires. Ignored when ``library_item`` is given."
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
                "Optional display name for the deployed VM; the server uses the "
                "OVF descriptor's name when omitted."
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
    "additionalProperties": False,
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
                "rather than a raw vendor error. Every non-``deployed`` status is "
                "reached before or without a successful mutation."
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
            "items": {"type": "object"},
            "description": (
                "Per-issue projections ``{category, severity, message}`` drawn "
                "from the OVF deploy report (errors / warnings / information) or "
                "the resolution / power-on failure. Empty on a clean deploy."
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
    "required": ["status", "vm_id", "powered_on", "issues"],
}


# ===========================================================================
# Host-domain write composites (#3182) -- dangerous / requires approval
# ===========================================================================


#: ``vmware.composite.host.datastore_mount_nfs`` parameter schema.
#:
#: Mount an NFS export as a datastore on a host via the **synchronous** vim
#: ``HostDatastoreSystem.CreateNasDatastore`` (builds a ``HostNasVolumeSpec``).
#: The host is selected by display name or moref; ``access_mode`` /
#: ``nfs_type`` map to the pinned spec's ``HostMountMode_enum`` /
#: ``HostNasVolumeSpec.type``.
HOST_DATASTORE_MOUNT_NFS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Host display name or moref (e.g. 'esxi-01.lab' or 'host-15'). "
                "Resolved via ``GET:/vcenter/host`` — a display-name lookup first, "
                "falling back to a moref match; an ambiguous name is refused with "
                "``status='ambiguous_host'`` before any write."
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
                "(``HostNasVolumeSpec.localPath``). A name already in use on the "
                "host faults vendor-side (``DuplicateName``)."
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
    "required": ["host", "nfs_server", "remote_path", "datastore_name"],
    "additionalProperties": False,
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
            "description": "Host display name or moref (see host.datastore_mount_nfs).",
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
    "required": ["host", "disk_uuids"],
    "additionalProperties": False,
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
            "description": "Host display name or moref (see host.datastore_mount_nfs).",
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
    "required": ["host", "service", "action"],
    "additionalProperties": False,
}


#: ``vmware.composite.host.datastore_mount_nfs`` response schema.
HOST_DATASTORE_MOUNT_NFS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["mounted", "host_not_found", "ambiguous_host", "config_manager_unreadable"],
            "description": (
                "``'mounted'`` — the datastore was created; the refusal statuses "
                "are reached before any write (host name/moref did not resolve "
                "uniquely, or the host's HostDatastoreSystem was unreadable)."
            ),
        },
        "host": {"type": "string", "description": "Resolved host moid (or the input on refusal)."},
        "datastore": {
            "type": ["string", "null"],
            "description": "New datastore moid; ``null`` on any non-``mounted`` status.",
        },
        "summary": {
            "type": ["object", "null"],
            "description": (
                "Datastore summary on success — datastore moid, name, and the "
                "resolved mount coordinates; ``null`` on refusal."
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
        "status": {
            "type": "string",
            "enum": [
                "marked",
                "partial",
                "host_not_found",
                "ambiguous_host",
                "config_manager_unreadable",
            ],
            "description": (
                "``'marked'`` — every disk reached SSD/HDD state; ``'partial'`` — "
                "at least one disk faulted / timed out / errored (see per-disk "
                "``results``); the refusal statuses are reached before any write."
            ),
        },
        "host": {"type": "string", "description": "Resolved host moid (or the input on refusal)."},
        "mode": {"type": "string", "enum": ["flash", "non_flash"]},
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
        "status": {
            "type": "string",
            "enum": [
                "applied",
                "service_not_allowed",
                "host_not_found",
                "ambiguous_host",
                "config_manager_unreadable",
            ],
            "description": (
                "``'applied'`` — the action (and optional policy update) ran; "
                "``'service_not_allowed'`` — the service is outside the curated "
                "allowlist (refused before any resolution or write); the remaining "
                "refusals are reached before any write."
            ),
        },
        "host": {"type": "string", "description": "Resolved host moid (or the input on refusal)."},
        "service": {"type": "string"},
        "action": {"type": ["string", "null"], "enum": ["start", "stop", "restart", None]},
        "policy": {"type": ["string", "null"], "enum": ["on", "automatic", "off", None]},
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
