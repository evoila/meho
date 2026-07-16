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

Ops NOT in the audited set (transport-node listing, segment listing,
tier-0 gateways, distributed-firewall policies + rules) stay as ingested
browse breadth -- enable-able through the generic review flow
(``ReviewService.enable_reads``); only the audited operational reads are
promoted to first-class typed ops. ``tier-1 gateway create`` (a write) is
out of scope: the first write on a read-only connector is its own
approval-gated G3.x write-surface initiative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NSX_TYPED_OPS",
    "NSX_TYPED_WHEN_TO_USE_BY_GROUP",
    "NsxTypedOp",
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
    safety_level: Literal["safe", "caution", "dangerous"]
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
        "(nsx.transport_zone.list) and per-tenant tier-1 gateways "
        "(nsx.tier1.list). Read-only."
    ),
    _GROUP_ALARMS: (
        "Use to read NSX system alarms (nsx.alarm.list) -- open faults and "
        "capacity/health events across the manager, with optional filters "
        "by status, feature, and severity. The right group when the "
        "question is 'what is NSX complaining about right now?'. Read-only."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the no-argument reads.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# nsx.node.status
# ---------------------------------------------------------------------------

_NODE_STATUS = NsxTypedOp(
    op_id="nsx.node.status",
    handler_attr="node_status",
    summary="NSX Manager node identity, version, and build.",
    description=(
        "Returns the NSX Manager node's identity and version via "
        "GET /api/v1/node: node_version (the NSX build the manager runs), "
        "kernel_version, node_uuid, hostname, and external_id. The probe / "
        "sanity read to confirm which NSX build a target points at and that "
        "the manager answers before any heavier policy read. Works with "
        "zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_HEALTH,
    tags=("read-only", "nsx", "manager", "version"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to identify the NSX Manager -- its build, version, node "
            "UUID, hostname -- as a probe before heavier reads or to confirm "
            "which NSX cluster the target points at."
        ),
        "output_shape": ("{node_version, kernel_version, node_uuid, hostname, external_id}."),
    },
)


# ---------------------------------------------------------------------------
# nsx.cluster.status
# ---------------------------------------------------------------------------

_CLUSTER_STATUS = NsxTypedOp(
    op_id="nsx.cluster.status",
    handler_attr="cluster_status",
    summary="NSX management + control cluster health.",
    description=(
        "Returns the NSX management-plane health via "
        "GET /api/v1/cluster/status: mgmt_cluster_status (overall "
        "management cluster state), control_cluster_status, and per-member "
        "detail. The read an operator runs when a control-plane outage is "
        "suspected. Works with zero catalog ingest. safety_level=safe, "
        "read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_HEALTH,
    tags=("read-only", "nsx", "manager", "cluster"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to confirm whether the NSX management plane is healthy and "
            "which members make up the cluster, e.g. when a control-plane "
            "outage is suspected."
        ),
        "output_shape": (
            "{mgmt_cluster_status, control_cluster_status, detail: [...]}. "
            "If unhealthy, surface the failing member's id."
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
        "Returns the NSX Manager automated-backup configuration via "
        "GET /api/v1/cluster/backups/config, with the retention-relevant "
        "fields surfaced and secret material scrubbed. backup_enabled is "
        "hoisted for the at-a-glance answer; passphrase_configured reports "
        "whether an encryption passphrase is set without returning it; the "
        "config object preserves backup_schedule (the accumulation rate -- "
        "a WeeklyBackupSchedule or IntervalBackupSchedule) and "
        "remote_file_server (server / port / directory_path / protocol -- "
        "where backups accumulate, with credentials masked), plus "
        "inventory_summary_interval and after_inventory_update_interval. "
        "The read for the disk-fill class of incident (Broadcom KB 442696 "
        "shape) where a frequent schedule against a bounded remote server "
        "fills the disk. Works with zero catalog ingest. safety_level=safe, "
        "read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={
        "type": "object",
        "properties": {
            "backup_enabled": {"type": ["boolean", "null"]},
            "passphrase_configured": {"type": "boolean"},
            "config": {"type": "object"},
        },
        "additionalProperties": True,
    },
    group_key=_GROUP_BACKUP,
    tags=("read-only", "nsx", "manager", "backup", "retention"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
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
            "config.remote_file_server.directory_path for the disk-fill risk."
        ),
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
        "Returns the current NSX Manager backup operation status via "
        "GET /api/v1/cluster/backups/status: whether a backup is running, "
        "the last operation's success/failure, and its timing "
        "(current_backup_operation_status). Pairs with nsx.backup.config so "
        "the operator sees both 'is backup configured (and how often)' and "
        "'did the last backup actually succeed'. Works with zero catalog "
        "ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_BACKUP,
    tags=("read-only", "nsx", "manager", "backup"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to confirm whether the last NSX Manager backup succeeded "
            "and when, or whether one is running now -- alongside "
            "nsx.backup.config for the schedule."
        ),
        "output_shape": (
            "{current_backup_operation_status: {...}}. Surface the last "
            "operation's success flag and timing."
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
        "Lists NSX transport zones via "
        "GET /policy/api/v1/infra/sites/default/enforcement-points/default/"
        "transport-zones -- the scope segments and tier-gateways attach to. "
        "Returns {results: [...]} where each zone carries id, display_name, "
        "tz_type (OVERLAY / VLAN), and host_switch_name. Works with zero "
        "catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "policy", "transport-zone"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to list transport zones under the default enforcement "
            "point -- the scope segments and tier-gateways attach to."
        ),
        "output_shape": ("{results: [{id, display_name, tz_type, host_switch_name}, ...]}."),
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
        "Lists NSX tier-1 gateways via GET /policy/api/v1/infra/tier-1s -- "
        "the per-tenant east-west routing surface attached under a tier-0. "
        "Returns {results: [...]} where each tier-1 carries id, "
        "display_name, tier0_path (its parent), route_advertisement_types, "
        "and ha_mode. Read-only inventory: tier-1 gateway *create* is a "
        "separate approval-gated write and is not registered here. Works "
        "with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "nsx", "policy", "tier1"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to inspect per-tenant tier-1 gateways -- useful when "
            "mapping which tier-1 fronts a given segment or cross-referencing "
            "tier0_path against the tier-0 listing."
        ),
        "output_shape": (
            "{results: [{id, display_name, tier0_path, route_advertisement_types, ha_mode}, ...]}."
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
        "Lists NSX system alarms via GET /api/v1/alarms -- open faults and "
        "capacity/health events across the manager. Optional status (OPEN / "
        "ACKNOWLEDGED / SUPPRESSED / RESOLVED), feature_name (e.g. "
        "manager_health), and severity (CRITICAL / HIGH / MEDIUM / LOW) "
        "narrow the result to the actionable set. Returns {results: [...]} "
        "where each alarm carries id, status, severity, feature_name, "
        "event_type, node_id, last_reported_time, and description. Works "
        "with zero catalog ingest. safety_level=safe, read-only."
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
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_ALARMS,
    tags=("read-only", "nsx", "manager", "alarms"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
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
    _ALARM_LIST,
)


async def register_nsx_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`NSX_TYPED_OPS` into ``endpoint_descriptor``.

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
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in NSX_TYPED_OPS:
        handler = getattr(NsxConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"NsxConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else NSX_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"NsxConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for "
                f"that key. Add an entry to NSX_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=NsxConnector.product,
            version=NsxConnector.version,
            impl_id=NsxConnector.impl_id,
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
        product=NsxConnector.product,
        version=NsxConnector.version,
        impl_id=NsxConnector.impl_id,
    )
