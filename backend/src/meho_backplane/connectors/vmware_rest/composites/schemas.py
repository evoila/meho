# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema 2020-12 parameter + response schemas for the 13 vmware-rest composites.

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
  each. The 8 write composites inherit T4's
  ``safety_level="dangerous"`` + ``requires_approval=True`` defaults
  (G3.1-T6 / #509). The schema text reflects which side of that line
  each composite sits on; the registration call site enforces the
  policy.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA",
    "CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA",
    "CLUSTER_PATCH_PARAMETER_SCHEMA",
    "CLUSTER_PATCH_RESPONSE_SCHEMA",
    "DATASTORE_USAGE_PARAMETER_SCHEMA",
    "DATASTORE_USAGE_RESPONSE_SCHEMA",
    "EVENT_TAIL_PARAMETER_SCHEMA",
    "EVENT_TAIL_RESPONSE_SCHEMA",
    "HOST_DETACH_FROM_VDS_PARAMETER_SCHEMA",
    "HOST_DETACH_FROM_VDS_RESPONSE_SCHEMA",
    "HOST_EVACUATE_PARAMETER_SCHEMA",
    "HOST_EVACUATE_RESPONSE_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA",
    "PERFORMANCE_SUMMARY_PARAMETER_SCHEMA",
    "PERFORMANCE_SUMMARY_RESPONSE_SCHEMA",
    "VM_CLONE_PARAMETER_SCHEMA",
    "VM_CLONE_RESPONSE_SCHEMA",
    "VM_CREATE_PARAMETER_SCHEMA",
    "VM_CREATE_RESPONSE_SCHEMA",
    "VM_MIGRATE_PARAMETER_SCHEMA",
    "VM_MIGRATE_RESPONSE_SCHEMA",
    "VM_POWER_BULK_PARAMETER_SCHEMA",
    "VM_POWER_BULK_RESPONSE_SCHEMA",
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
                "datastores whose name appears in this list are surfaced. "
                "Empty / absent returns every datastore the operator can "
                "see."
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
                            "Total capacity in bytes; ``null`` when the detail payload omits it."
                        ),
                    },
                    "free_space": {
                        "type": ["integer", "null"],
                        "description": (
                            "Free space in bytes; ``null`` when the detail payload omits it."
                        ),
                    },
                    "vm_count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Number of VMs placed on this datastore.",
                    },
                    "vm_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of VMs placed on this datastore.",
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
# The 8 write composites inherit T4's ``safety_level="dangerous"`` +
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
