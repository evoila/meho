# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`NsxConnector` (#2302).

The audited NSX read set is registered as typed ops
(``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog ingest** -- the #2247 failure class the older ingested-row
enablement was subject to (per-deploy catalog state). The op bodies live in
:mod:`meho_backplane.connectors.nsx.typed_reads`; thin bound-method shims
on :class:`~meho_backplane.connectors.nsx.connector.NsxConnector` expose
them under the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk
recovers each callable from its persisted ``module.ClassName.method``
path.

The dataclass + tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.argocd.ops` and
:mod:`meho_backplane.connectors.vmware_rest.typed_ops`.

Ops NOT in the audited set (tier-0 gateways,
distributed-firewall policies + rules) stay as ingested browse breadth
-- enable-able through the generic review flow
(``ReviewService.enable_reads``); only the audited operational reads are
promoted to first-class typed ops. Segment / tier-1 gateway *create*
(a write) is out of scope: the first write on a read-only connector is
its own approval-gated G3.x write-surface initiative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from meho_backplane.connectors.nsx.connector import NsxConnector
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NSX_TYPED_OPS",
    "NSX_TYPED_WHEN_TO_USE_BY_GROUP",
    "NsxTypedOp",
    "register_nsx4_typed_operations",
    "register_nsx_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys. Each groups the audited reads by the operator
# question they answer; the ``when_to_use`` blurbs below are what the
# agent reads verbatim through ``list_operation_groups`` to pick a group.
_GROUP_HEALTH = "nsx-health"
_GROUP_BACKUP = "nsx-backup"
_GROUP_INVENTORY = "nsx-inventory"
_GROUP_ALARMS = "nsx-alarms"


@dataclass(frozen=True)
class NsxTypedOp:
    """Metadata for one NSX typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_nsx_typed_operations` can splat the
    dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on
    :class:`~meho_backplane.connectors.nsx.connector.NsxConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class at registration time. Mirrors
    :class:`~meho_backplane.connectors.argocd.ops.ArgoCdOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per typed-op group.
#: ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set (typed_register ``_validate_when_to_use_pairing``).
NSX_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_HEALTH: (
        "Use to read the NSX management plane's health and version: the "
        "manager node's build/version/UUID (nsx.node.status) and the "
        "management + control cluster status (nsx.cluster.status). The "
        "right group for the probe / incident-triage question 'which NSX "
        "build is this and is the manager cluster healthy?'. Read-only."
    ),
    _GROUP_BACKUP: (
        "Use to inspect NSX Manager backup: the backup configuration and "
        "schedule (nsx.backup.config -- whether automated backup is "
        "enabled, how often it runs, and which remote file server it "
        "writes to) and the current backup operation status "
        "(nsx.backup.status). The right group when diagnosing a backup "
        "gap or a retention/schedule misconfiguration that risks filling "
        "the remote server's disk. Read-only; the backup passphrase is "
        "never returned."
    ),
    _GROUP_INVENTORY: (
        "Use to list the NSX routing/overlay inventory the operator asks "
        "about most: transport zones under the default enforcement point "
        "(nsx.transport_zone.list), per-tenant tier-1 gateways "
        "(nsx.tier1.list), overlay/VLAN segments with their "
        "subnet/CIDR occupancy (nsx.segment.list) -- the pre-flight read "
        "before carving a new segment -- and edge/host transport nodes "
        "(nsx.transport_node.list), with per-node fabric health via "
        "nsx.transport_node.state, the edge-health pre-flight before a "
        "maintenance-mode or failover action. Read-only."
    ),
    _GROUP_ALARMS: (
        "Use to read NSX system alarms (nsx.alarm.list) -- open faults and "
        "capacity/health events across the manager, with optional filters "
        "by status, feature, and severity. The right group when the "
        "question is 'what is NSX complaining about right now?'. Read-only."
    ),
}


# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the no-argument reads. The one
#: example is the empty call.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def _field(type_: str | list[str], description: str) -> dict[str, Any]:
    """Return one described response field."""
    return {"type": type_, "description": description}


def _results_envelope(item_fields: dict[str, Any], what: str) -> dict[str, Any]:
    """Return the NSX ``{results: [...], result_count}`` list envelope schema."""
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": f"The {what}.",
                "items": {
                    "type": "object",
                    "properties": item_fields,
                    "additionalProperties": True,
                },
            },
            "result_count": _field("integer", f"How many {what} NSX reports."),
        },
        "additionalProperties": True,
    }


_ID = _field("string", "The NSX id of the object.")
_DISPLAY_NAME = _field("string", "The name shown in the NSX UI.")
_PATH = _field("string", "The policy path of the object.")


# ---------------------------------------------------------------------------
# nsx.node.status
# ---------------------------------------------------------------------------

_NODE_STATUS = NsxTypedOp(
    op_id="nsx.node.status",
    handler_attr="node_status",
    summary="NSX Manager node identity, version, and build.",
    description=(
        "Reads the NSX Manager node (GET /api/v1/node): the NSX version, kernel "
        "version, node UUID, host name and external id. Use it to check which NSX "
        "build a target runs and that the manager answers. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={
        "type": "object",
        "properties": {
            "node_version": _field("string", "The NSX version the manager runs."),
            "kernel_version": _field("string", "The kernel version of the node."),
            "node_uuid": _field("string", "The UUID of the manager node."),
            "hostname": _field("string", "The host name of the manager node."),
            "external_id": _field("string", "The external id of the node."),
        },
        "additionalProperties": True,
    },
    group_key=_GROUP_HEALTH,
    tags=("read-only", "nsx", "manager", "version"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to identify the NSX Manager -- its build, version, node "
            "UUID, hostname -- as a probe before heavier reads or to confirm "
            "which NSX cluster the target points at."
        ),
        "output_shape": "{node_version, kernel_version, node_uuid, hostname, external_id}.",
        "next_step": (
            "Call nsx.cluster.status to check that the manager cluster is healthy, "
            "or nsx.alarm.list to see open faults."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.cluster.status
# ---------------------------------------------------------------------------

_HEALTH_FIELD_NOTE = "An NSX status code; the lowercase 'unknown' means NSX did not report it."

_CLUSTER_STATUS = NsxTypedOp(
    op_id="nsx.cluster.status",
    handler_attr="cluster_status",
    summary="NSX management + control cluster health.",
    description=(
        "Reads the NSX cluster health (GET /api/v1/cluster/status). Returns the NSX "
        "body unchanged, plus flat fields MEHO adds: health_overall, health_mgmt, "
        "health_control and group_status_counts. Read health_overall first. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={
        "type": "object",
        "properties": {
            "health_overall": _field(
                "string",
                "Added by MEHO: overall_status, else detailed_cluster_status."
                f"overall_status (STABLE / DEGRADED / UNAVAILABLE). {_HEALTH_FIELD_NOTE}",
            ),
            "health_mgmt": _field(
                "string",
                "Added by MEHO: mgmt_cluster_status.status (deprecated by NSX). "
                + _HEALTH_FIELD_NOTE,
            ),
            "health_control": _field(
                "string",
                "Added by MEHO: control_cluster_status.status (deprecated by NSX). "
                + _HEALTH_FIELD_NOTE,
            ),
            "group_status_counts": {
                "type": "object",
                "description": (
                    "Added by MEHO: how many cluster groups have each group_status "
                    "(STABLE / DEGRADED / UNSTABLE / UNAVAILABLE / unknown)."
                ),
                "additionalProperties": {"type": "integer"},
            },
            "overall_status": _field("string", "The NSX overall cluster status."),
            "detailed_cluster_status": _field(
                "object", "NSX detail: overall_status and the groups list with group_status."
            ),
            "node_services_status": _field("object", "NSX status of the node services."),
            "mgmt_cluster_status": _field("object", "NSX management cluster status (deprecated)."),
            "control_cluster_status": _field("object", "NSX control cluster status (deprecated)."),
        },
        "additionalProperties": True,
    },
    group_key=_GROUP_HEALTH,
    tags=("read-only", "nsx", "manager", "cluster"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to confirm whether the NSX management plane is healthy and "
            "which members make up the cluster, e.g. when a control-plane "
            "outage is suspected."
        ),
        "output_shape": (
            "{health_overall, health_mgmt, health_control, "
            "group_status_counts: {STATUS: count}, overall_status, "
            "detailed_cluster_status: {overall_status, groups: [...]}, "
            "node_services_status, mgmt_cluster_status, "
            "control_cluster_status}. Read health_overall first; a value "
            "other than STABLE, or any non-STABLE key in "
            "group_status_counts, means unhealthy -- then surface the "
            "failing group from detailed_cluster_status.groups. 'unknown' "
            "means NSX did not report that field; never treat it as healthy. "
            "A vendor uppercase UNKNOWN is passed through as is. Prefer "
            "health_overall: the mgmt and control fields are deprecated by NSX."
        ),
        "next_step": (
            "If a group is not STABLE, call nsx.alarm.list with status='OPEN' to "
            "find the fault behind it."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.backup.config
# ---------------------------------------------------------------------------

_BACKUP_CONFIG = NsxTypedOp(
    op_id="nsx.backup.config",
    handler_attr="backup_config",
    summary="NSX Manager automated-backup configuration (retention-relevant fields surfaced).",
    description=(
        "Reads the NSX Manager backup configuration (GET /api/v1/cluster/backups/"
        "config): whether backup is on, the schedule, and the remote file server. "
        "Passwords and the passphrase are masked. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={
        "type": "object",
        "properties": {
            "backup_enabled": _field(
                ["boolean", "null"], "Whether automated backup is on; null if NSX did not say."
            ),
            "passphrase_configured": _field(
                "boolean", "Whether a backup passphrase is set. The passphrase is never returned."
            ),
            "config": {
                "type": "object",
                "description": "The NSX backup configuration, with secrets masked.",
                "properties": {
                    "backup_schedule": _field(
                        "object", "How often backups run (weekly or interval schedule)."
                    ),
                    "remote_file_server": _field(
                        "object", "Where backups go: server, port, directory_path, protocol."
                    ),
                    "inventory_summary_interval": _field(
                        "integer", "Seconds between inventory summary backups."
                    ),
                    "after_inventory_update_interval": _field(
                        "integer", "Seconds to wait after an inventory change before a backup."
                    ),
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    },
    group_key=_GROUP_BACKUP,
    tags=("read-only", "nsx", "manager", "backup", "retention"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check whether NSX Manager backups are configured and how "
            "often they run, and to which remote server -- e.g. when "
            "diagnosing a missing-backup gap or a retention/schedule "
            "misconfiguration that risks filling the remote server's disk."
        ),
        "output_shape": (
            "{backup_enabled, passphrase_configured, config: {backup_schedule, "
            "remote_file_server, inventory_summary_interval, "
            "after_inventory_update_interval, ...}}. The passphrase and any "
            "nested SFTP credential are masked as ***REDACTED***; read "
            "config.backup_schedule (frequency) against "
            "config.remote_file_server.directory_path for the disk-fill risk "
            "(Broadcom KB 442696)."
        ),
        "next_step": ("Call nsx.backup.status to see whether a backup is running now."),
    },
)


# ---------------------------------------------------------------------------
# nsx.backup.status
# ---------------------------------------------------------------------------

_BACKUP_STATUS = NsxTypedOp(
    op_id="nsx.backup.status",
    handler_attr="backup_status",
    summary="NSX Manager current backup operation status.",
    description=(
        "Reads the status of the running NSX Manager backup (GET /api/v1/cluster/"
        "backups/status): whether a backup runs now, its step and its times. Pairs "
        "with nsx.backup.config. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={
        "type": "object",
        "properties": {
            "operation_type": _field(
                "string", "NONE (no backup runs now; other fields are then empty) or BACKUP."
            ),
            "backup_id": _field("string", "The id of the running backup."),
            "current_step": _field("string", "The step the running backup is on."),
            "current_step_message": _field("string", "A readable note on the current step."),
            "start_time": _field("integer", "When the backup started, in epoch milliseconds."),
            "end_time": _field("integer", "When the backup ended, in epoch milliseconds."),
        },
        "additionalProperties": True,
    },
    group_key=_GROUP_BACKUP,
    tags=("read-only", "nsx", "manager", "backup"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see whether an NSX Manager backup is running now, and "
            "which step it is on -- alongside nsx.backup.config for the schedule."
        ),
        "output_shape": (
            "The NSX body unchanged: {operation_type, backup_id, current_step, "
            "current_step_message, start_time, end_time}. operation_type NONE "
            "means no backup runs now."
        ),
        "next_step": (
            "If backups seem to be missing, call nsx.backup.config to check that "
            "backup is on and how often it runs."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.transport_zone.list
# ---------------------------------------------------------------------------

_TRANSPORT_ZONE_LIST = NsxTypedOp(
    op_id="nsx.transport_zone.list",
    handler_attr="transport_zone_list",
    summary="NSX transport zones under the default enforcement point.",
    description=(
        "Lists the NSX transport zones under the default site and enforcement "
        "point (policy API). Segments and gateways attach to these zones. Each "
        "zone has id, display_name, path and tz_type (OVERLAY_BACKED or VLAN_BACKED). "
        "Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_results_envelope(
        {
            "id": _ID,
            "display_name": _DISPLAY_NAME,
            "path": _PATH,
            "tz_type": _field(
                "string",
                "The zone type: OVERLAY_BACKED or VLAN_BACKED. Older NSX may send "
                "OVERLAY_STANDARD or OVERLAY_ENS.",
            ),
        },
        "transport zones",
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "policy", "transport-zone"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to list transport zones under the default enforcement "
            "point -- the scope segments and tier-gateways attach to."
        ),
        "output_shape": "{results: [{id, display_name, path, tz_type}, ...]}.",
        "next_step": (
            "Call nsx.segment.list and match transport_zone_path to see which segments use a zone."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.tier1.list
# ---------------------------------------------------------------------------

_TIER1_LIST = NsxTypedOp(
    op_id="nsx.tier1.list",
    handler_attr="tier1_list",
    summary="NSX per-tenant tier-1 gateway inventory.",
    description=(
        "Lists the NSX tier-1 gateways (GET /policy/api/v1/infra/tier-1s): the "
        "per-tenant routers under a tier-0. Each has id, display_name, tier0_path, "
        "route_advertisement_types and ha_mode. Read-only; creating a tier-1 is "
        "not offered here."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_results_envelope(
        {
            "id": _ID,
            "display_name": _DISPLAY_NAME,
            "path": _PATH,
            "tier0_path": _field("string", "The policy path of the parent tier-0 gateway."),
            "route_advertisement_types": {
                "type": "array",
                "description": "The route types this tier-1 advertises.",
                "items": {"type": "string"},
            },
            "ha_mode": _field("string", "The high-availability mode of the gateway."),
        },
        "tier-1 gateways",
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "policy", "tier1"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to inspect per-tenant tier-1 gateways -- useful when "
            "mapping which tier-1 fronts a given segment or cross-referencing "
            "tier0_path against the tier-0 listing."
        ),
        "output_shape": (
            "{results: [{id, display_name, tier0_path, route_advertisement_types, ha_mode}, ...]}."
        ),
        "next_step": (
            "Call nsx.segment.list to see the segments; a segment's "
            "connectivity_path names the tier-1 it attaches to."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.segment.list
# ---------------------------------------------------------------------------

_SEGMENT_LIST = NsxTypedOp(
    op_id="nsx.segment.list",
    handler_attr="segment_list",
    summary="NSX overlay/VLAN segment inventory with subnet/CIDR occupancy.",
    description=(
        "Lists the NSX segments (GET /policy/api/v1/infra/segments) with their "
        "transport zone and subnets. Use it before you create a segment, to see "
        "which CIDRs are already in use. Read-only; creating a segment is not "
        "offered here."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_results_envelope(
        {
            "id": _ID,
            "display_name": _DISPLAY_NAME,
            "path": _PATH,
            "resource_type": _field("string", "The NSX object type, for example Segment."),
            "transport_zone_path": _field(
                "string", "The policy path of the transport zone the segment uses."
            ),
            "connectivity_path": _field(
                "string", "The policy path of the gateway the segment attaches to."
            ),
            "subnets": {
                "type": "array",
                "description": "The subnets of the segment.",
                "items": {
                    "type": "object",
                    "properties": {
                        "gateway_address": _field(
                            "string",
                            "The gateway address in CIDR form: the gateway IP, a slash, then "
                            "the prefix length.",
                        ),
                        "network": _field("string", "The network address in CIDR form."),
                    },
                    "additionalProperties": True,
                },
            },
        },
        "segments",
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "policy", "segment"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before carving a new NSX overlay segment or attaching a "
            "workload, to list existing segments and read which subnet/gateway "
            "CIDRs are already in use (avoid a CIDR collision)."
        ),
        "output_shape": (
            "{results: [{id, display_name, resource_type, transport_zone_path, "
            "subnets: [{gateway_address}]}, ...], result_count}. The NSX "
            "envelope passes through unchanged. Read subnets[].gateway_address "
            "across segments for the CIDR occupancy the pre-flight needs."
        ),
        "next_step": (
            "Call nsx.transport_zone.list to resolve transport_zone_path, or "
            "nsx.tier1.list to resolve the gateway a segment attaches to."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.transport_node.list
# ---------------------------------------------------------------------------

_TRANSPORT_NODE_LIST = NsxTypedOp(
    op_id="nsx.transport_node.list",
    handler_attr="transport_node_list",
    summary="NSX edge/host transport-node fabric inventory (state via nsx.transport_node.state).",
    description=(
        "Lists the NSX transport nodes (GET /api/v1/transport-nodes): the edge and "
        "host nodes that carry the overlay. Each has id, display_name and "
        "node_deployment_info.resource_type. The list has no health; use "
        "nsx.transport_node.state for that. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=_results_envelope(
        {
            "id": _ID,
            "display_name": _DISPLAY_NAME,
            "node_deployment_info": {
                "type": "object",
                "description": "How the node is deployed.",
                "properties": {
                    "resource_type": _field(
                        "string",
                        "EdgeNode for an edge node, HostNode for a host node, or "
                        "PublicCloudGatewayNode.",
                    ),
                },
                "additionalProperties": True,
            },
        },
        "transport nodes",
    ),
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "manager", "transport-node"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to list edge/host transport nodes and get their ids -- the "
            "entry point for the per-node health read nsx.transport_node.state. "
            "Tell edge from host nodes on node_deployment_info.resource_type."
        ),
        "output_shape": (
            "{results: [{id, display_name, node_deployment_info: "
            "{resource_type}}, ...], result_count}. The list carries no live "
            "state -- feed each id to nsx.transport_node.state for health."
        ),
        "next_step": (
            "Call nsx.transport_node.state with a node id before a "
            "maintenance-mode or failover action."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.transport_node.state
# ---------------------------------------------------------------------------

_TRANSPORT_NODE_STATE = NsxTypedOp(
    op_id="nsx.transport_node.state",
    handler_attr="transport_node_state",
    summary="Realization + maintenance-mode + tunnel state of one NSX transport node.",
    description=(
        "Reads the state of one transport node (GET /api/v1/transport-nodes/"
        "{transport-node-id}/state): its overall state, maintenance mode and "
        "host-switch state. Needs a node id from nsx.transport_node.list. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The transport-node id (from nsx.transport_node.list).",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<transport-node-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "transport_node_id": _field("string", "The id of the transport node."),
            "state": _field("string", "The overall state of the node, for example success."),
            "maintenance_mode_state": _field(
                "string", "Whether the node is in, entering or out of maintenance mode."
            ),
            "node_deployment_state": _field("object", "The deployment state of the node."),
            "deployment_progress_state": _field("object", "The progress of a running deployment."),
            "host_switch_states": {
                "type": "array",
                "description": "The tunnel and connection state per host switch.",
                "items": {"type": "object"},
            },
            "failure_message": _field("string", "Why the node failed, when it did."),
        },
        "additionalProperties": True,
    },
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "manager", "transport-node", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call with a transport-node id before a maintenance-mode or "
            "failover action, to confirm the node (and its HA peer) is "
            "realized, not already in maintenance, and tunnel-healthy. A "
            "degraded surviving edge can cost a tier-1 gateway its HA."
        ),
        "parameter_hints": {"id": "The transport-node id from nsx.transport_node.list."},
        "output_shape": (
            "{state, maintenance_mode_state, node_deployment_state, "
            "deployment_progress_state, host_switch_states: [...]}. The NSX "
            "body passes through unchanged. Read maintenance_mode_state + "
            "state before draining the peer edge."
        ),
        "next_step": (
            "Read the same state for the HA peer edge. If a node is not "
            "healthy, call nsx.alarm.list to find the fault."
        ),
    },
)


# ---------------------------------------------------------------------------
# nsx.alarm.list
# ---------------------------------------------------------------------------

_ALARM_LIST = NsxTypedOp(
    op_id="nsx.alarm.list",
    handler_attr="alarm_list",
    summary="NSX system alarms, optionally filtered by status/feature/severity.",
    description=(
        "Lists NSX system alarms (GET /api/v1/alarms): open faults and health "
        "events across the manager. You can filter by status, feature_name and "
        "severity. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["OPEN", "ACKNOWLEDGED", "SUPPRESSED", "RESOLVED"],
                "description": "Optional alarm status filter. Omit for all statuses.",
            },
            "feature_name": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional NSX feature filter (e.g. 'manager_health'). Omit for all features."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                "description": "Optional severity filter. Omit for all severities.",
            },
        },
        "additionalProperties": False,
        "examples": [{"status": "OPEN", "severity": "CRITICAL"}],
    },
    response_schema=_results_envelope(
        {
            "id": _ID,
            "status": _field("string", "OPEN, ACKNOWLEDGED, SUPPRESSED or RESOLVED."),
            "severity": _field("string", "CRITICAL, HIGH, MEDIUM or LOW."),
            "feature_name": _field("string", "The NSX feature that raised the alarm."),
            "event_type": _field("string", "The kind of event within the feature."),
            "node_id": _field("string", "The id of the node the alarm is about."),
            "last_reported_time": _field(
                "integer", "When NSX last reported the alarm, in epoch milliseconds."
            ),
            "description": _field("string", "What the alarm means."),
            "recommended_action": _field("string", "What NSX suggests you do."),
        },
        "alarms",
    ),
    group_key=_GROUP_ALARMS,
    tags=("read-only", "nsx", "manager", "alarms"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when the question is 'what is NSX complaining about right "
            "now?'. Narrow with status='OPEN' for active faults, or by "
            "feature_name / severity when the operator named one."
        ),
        "parameter_hints": {
            "status": "OPEN / ACKNOWLEDGED / SUPPRESSED / RESOLVED; omit for all.",
            "feature_name": "NSX feature id (e.g. manager_health); omit for all.",
            "severity": "CRITICAL / HIGH / MEDIUM / LOW; omit for all.",
        },
        "output_shape": (
            "{results: [{id, status, severity, feature_name, event_type, "
            "node_id, last_reported_time, description}, ...]}."
        ),
        "next_step": (
            "For a cluster alarm call nsx.cluster.status; for an edge or host "
            "alarm call nsx.transport_node.state with the node_id."
        ),
    },
)


#: The typed ops :class:`NsxConnector` registers at lifespan startup --
#: the audited read set (#2302). Ordered health -> backup -> inventory ->
#: alarms to match the operator's typical triage path.
NSX_TYPED_OPS: tuple[NsxTypedOp, ...] = (
    _NODE_STATUS,
    _CLUSTER_STATUS,
    _BACKUP_CONFIG,
    _BACKUP_STATUS,
    _TRANSPORT_ZONE_LIST,
    _TIER1_LIST,
    _SEGMENT_LIST,
    _TRANSPORT_NODE_LIST,
    _TRANSPORT_NODE_STATE,
    _ALARM_LIST,
)


async def register_nsx_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`NSX_TYPED_OPS` under ``nsx-rest-9.0``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors`
    has walked every connector subpackage, so the descriptor rows land
    before the first dispatch. Idempotent across pod restarts (the helper
    skips the embedding recompute on unchanged summary / description /
    tags). Mirrors :func:`register_vmware_typed_operations` and the argocd
    typed-op registrar.

    The ``embedding_service`` keyword-only parameter is the runner
    contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar,
    so each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests
    # should not pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.nsx.connector import NsxConnector

    await _register_nsx_typed_operations_for(NsxConnector, embedding_service=embedding_service)


async def register_nsx4_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """The same typed ops under ``nsx-rest-4.2``, where NSX 4.x targets resolve.

    The op ids are the same on both implementations; rows are keyed by
    ``(product, version, impl_id, op_id)``, so they never collide. The
    dispatcher binds the inherited :class:`NsxConnector` handlers to the
    resolved :class:`Nsx4Connector` instance.
    """
    from meho_backplane.connectors.nsx.connector_4x import Nsx4Connector

    await _register_nsx_typed_operations_for(Nsx4Connector, embedding_service=embedding_service)


async def _register_nsx_typed_operations_for(
    connector_cls: type[NsxConnector],
    *,
    embedding_service: EmbeddingService | None,
) -> None:
    """Upsert every op in :data:`NSX_TYPED_OPS` under *connector_cls*'s triple."""
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in NSX_TYPED_OPS:
        handler = getattr(connector_cls, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"{connector_cls.__name__} typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else NSX_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"{connector_cls.__name__} typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to NSX_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=connector_cls.product,
            version=connector_cls.version,
            impl_id=connector_cls.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "nsx_typed_operations_registered",
        count=len(NSX_TYPED_OPS),
        product=connector_cls.product,
        version=connector_cls.version,
        impl_id=connector_cls.impl_id,
    )
