# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema 2020-12 parameter + response schemas for the 18 vmware-rest composites.

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
  each. The 13 write composites inherit T4's
  ``safety_level="dangerous"`` + ``requires_approval=True`` defaults
  (G3.1-T6 / #509, single-VM ``vm.power`` / #2301, the mutating
  VI-JSON ``vm.disk.grow`` / #2893, the folder-template
  ``vm.clone_from_template`` / #2894, and the vim cluster / inventory
  writes ``cluster.drs_rule.create`` + ``folder.create`` / #2895). The schema
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
    "HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA",
    "HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA",
    "HOST_EVACUATE_PARAMETER_SCHEMA",
    "HOST_EVACUATE_RESPONSE_SCHEMA",
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
    "VM_DISK_GROW_PARAMETER_SCHEMA",
    "VM_DISK_GROW_RESPONSE_SCHEMA",
    "VM_MIGRATE_PARAMETER_SCHEMA",
    "VM_MIGRATE_RESPONSE_SCHEMA",
    "VM_POWER_BULK_PARAMETER_SCHEMA",
    "VM_POWER_BULK_RESPONSE_SCHEMA",
    "VM_POWER_PARAMETER_SCHEMA",
    "VM_POWER_RESPONSE_SCHEMA",
    "VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA",
    "VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA",
]


#: ``vmware.composite.cluster.drs_recommendations`` parameter schema.
#:
#: Reads cluster summary + DRS state (optionally surfacing
#: ``recommendations_history`` from the DRS payload when present). The
#: composite dispatches one ``GET:/vcenter/cluster/{cluster}`` and one
#: ``GET:/vcenter/cluster/{cluster}/drs`` to a single target.
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
                "When true, the handler will also surface the historical "
                "recommendation summary from the DRS sub-op response. "
                "Read-only on either setting; the flag toggles aggregation "
                "shape, not the underlying calls."
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
                "Optional Distributed-Virtual-Switch managed-object ID. "
                "When supplied, scopes the distributed-switch listing "
                "(and thus the parent-DVS name enrichment) to this DVS. "
                "Distributed portgroups are listed via the generic "
                "network resource, which has no per-DVS filter, so the "
                "returned portgroup set is not narrowed by this value."
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
                "DRS configuration payload from "
                "``GET:/vcenter/cluster/{cluster}/drs`` (vSphere REST "
                "owns the inner shape)."
            ),
        },
        "recommendations_history": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Optional history slice surfaced from the DRS payload "
                "when ``include_recommendations_history=True``. Always "
                "a list when present; absent otherwise."
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
                            "the best-effort VM-placement enrichment was skipped "
                            "because its sub-call errored (see ``enrichment_note``)."
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
                            "skipped; records the failing sub-op, its status, and "
                            "the underlying error (status code + URL where the "
                            "sub-op carried them)."
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
                            "Parent DVS display name resolved via the "
                            "DVS listing; ``null`` when the parent DVS "
                            "is unknown or unnamed."
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
#: created VM via ``DELETE:/vcenter/vm/{vm}``.
VM_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folder_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Display name of the target VM folder. Resolved via "
                "``GET:/vcenter/folder?filter.names=...`` to the moid "
                "passed to ``POST:/vcenter/vm``."
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
                "ConfigSpec.guestOS field on ``POST:/vcenter/vm``."
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
                },
                "required": ["network"],
            },
            "default": [],
            "description": (
                "Per-NIC spec. Each entry drives a "
                "``PATCH:/vcenter/vm/{vm}/network`` after the VM is "
                "created. Empty list creates the VM with no NICs."
            ),
        },
        "power_on_after_create": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, the handler issues "
                "``POST:/vcenter/vm/{vm}/power?action=start`` after "
                "NIC attach. Default false leaves the VM powered-off."
            ),
        },
    },
    "required": ["folder_name", "name", "guest_os"],
    "additionalProperties": False,
}


#: ``vmware.composite.vm.clone`` parameter schema.
#:
#: Orchestrates a content-library deploy. Long-running: blocks until
#: the vSphere task completes or ``timeout_seconds`` elapses. The
#: caller can opt into fire-and-forget via ``wait_for_completion=False``.
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
                "Content-library template item id. Passed to "
                "``POST:/vcenter/vm-template/library-items?action=deploy``."
            ),
        },
        "wait_for_completion": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (default), block on the vSphere task until "
                "``timeout_seconds`` elapses. When false, return "
                "immediately with the task id for caller-side polling."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "default": 600,
            "description": (
                "Upper bound on the task wait when "
                "``wait_for_completion=True``. On timeout the composite "
                "returns ``status='timeout'`` with the task id; the "
                "task itself may still complete in the background."
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
        "patch_method": {
            "type": "string",
            "minLength": 1,
            "default": "default",
            "description": (
                "Patch backend selector. The handler forwards the "
                "string verbatim to the per-host patch sub-op so vendor "
                "patch flows can dispatch into ``vlcm`` / ``vum`` / "
                "``firmware`` without changing the composite's contract."
            ),
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
                "``create``, ``nic_attach``, ``power_on``."
            ),
        },
        "failed_step": {
            "type": ["string", "null"],
            "description": (
                "Name of the first failing step on rollback; ``null`` when ``status='created'``."
            ),
        },
        "rollback_reason": {
            "type": ["string", "null"],
            "description": (
                "Human-readable explanation of the rollback trigger; "
                "``null`` when ``status='created'``."
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
            "enum": ["completed", "pending", "timeout"],
            "description": (
                "``'completed'`` when the deploy task finished and "
                "wait_for_completion was true; ``'pending'`` when "
                "wait_for_completion was false (caller-side polling); "
                "``'timeout'`` when wait_for_completion expired."
            ),
        },
        "task_id": {
            "type": "string",
            "description": (
                "vSphere task id from the deploy. Always present so callers can poll independently."
            ),
        },
        "vm_id": {
            "type": ["string", "null"],
            "description": (
                "New VM moid surfaced when the task completed. ``null`` on pending/timeout."
            ),
        },
        "guidance": {
            "type": ["string", "null"],
            "description": (
                "Operator-facing next-step hint on non-completed "
                "statuses; ``null`` when ``status='completed'``."
            ),
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
            "enum": ["reverted", "ambiguous", "not_found"],
            "description": (
                "``'reverted'`` on a successful revert; "
                "``'ambiguous'`` when multiple snapshots share the "
                "name; ``'not_found'`` when no snapshot matches."
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
            "enum": ["detached", "incomplete"],
            "description": (
                "``'detached'`` -- every NIC migrated and the host "
                "removed from the DVS; ``'incomplete'`` -- one or more "
                "NIC migrations failed, the DVS detach was skipped."
            ),
        },
        "host": {
            "type": "string",
            "description": "Host moid the operator targeted.",
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
