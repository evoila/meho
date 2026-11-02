# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The first read composites: DRS settings, events, performance, datastore
usage and refresh, port group audit.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._guest_event_perf_llm_instructions import (
    GUEST_EVENT_PERF_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_refresh_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_RECOMMENDATIONS_PARAMETER_SCHEMA,
    CLUSTER_DRS_RECOMMENDATIONS_RESPONSE_SCHEMA,
    DATASTORE_REFRESH_PARAMETER_SCHEMA,
    DATASTORE_REFRESH_RESPONSE_SCHEMA,
    DATASTORE_USAGE_PARAMETER_SCHEMA,
    DATASTORE_USAGE_RESPONSE_SCHEMA,
    EVENT_TAIL_PARAMETER_SCHEMA,
    EVENT_TAIL_RESPONSE_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_PARAMETER_SCHEMA,
    NETWORK_PORTGROUP_AUDIT_RESPONSE_SCHEMA,
    PERFORMANCE_SUMMARY_PARAMETER_SCHEMA,
    PERFORMANCE_SUMMARY_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
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
        classifier_other=True,
    ),
    CompositeSpec(
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
        classifier_other=True,
    ),
    CompositeSpec(
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
    CompositeSpec(
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
        classifier_other=True,
    ),
    CompositeSpec(
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
    CompositeSpec(
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
        classifier_other=True,
    ),
)
