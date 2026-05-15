# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema 2020-12 parameter schemas for the 5 vmware-rest read composites.

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
* All five composites are read-only -- no schema mentions write
  semantics; the registration call site pins ``safety_level="safe"``
  and ``requires_approval=False`` on each.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA",
    "DATASTORE_USAGE_PARAMETER_SCHEMA",
    "EVENT_TAIL_PARAMETER_SCHEMA",
    "NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA",
    "PERFORMANCE_SUMMARY_PARAMETER_SCHEMA",
]


#: ``vmware.composite.cluster.drs_recommendations`` parameter schema.
#:
#: Reads cluster DRS state + active recommendations. The composite
#: dispatches one ``GET:/vcenter/cluster/{cluster}`` and one
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
                "When supplied, only portgroups belonging to this DVS "
                "are returned."
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
