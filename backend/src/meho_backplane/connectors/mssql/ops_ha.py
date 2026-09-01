# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""High-availability-group ops (AG / FCI / sync-health) for :class:`MssqlConnector`.

Registered by :meth:`MssqlConnector.register_operations` via the
:data:`~meho_backplane.connectors.mssql.ops.MSSQL_OPS` composer. See
:mod:`meho_backplane.connectors.mssql.ops` for the ``MssqlOp`` dataclass and
the shared schema fragments.
"""

from __future__ import annotations

from meho_backplane.connectors.mssql.ops import (
    _LIST_RESPONSE_SCHEMA,
    _NO_PARAMS,
    TDS_TRANSPORT_NOTE,
    MssqlOp,
)

HA_OPS: tuple[MssqlOp, ...] = (
    MssqlOp(
        op_id="mssql.ha.availability-groups",
        handler_attr="ha_availability_groups",
        summary="List Availability Groups with per-replica operational/sync state.",
        description=(
            "Returns one row per AG replica: AG name, backup preference, "
            "failure-condition level, health-check timeout, replica server "
            "name, availability/failover mode, and the replica's role, "
            "operational state, connected state, synchronization health, and "
            "recovery health (sys.availability_groups + sys.availability_replicas "
            "+ sys.dm_hadr_availability_replica_states). The migration-validation "
            "read for AG topology + health. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="ha",
        tags=("read-only", "ha", "availability-group", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to validate Availability Group topology and per-replica "
                "health (roles, sync health) before a migration cutover or "
                "failover. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{ag_name, automated_backup_preference_desc, "
                "failure_condition_level, health_check_timeout, "
                "replica_server_name, availability_mode_desc, "
                "failover_mode_desc, role_desc, operational_state_desc, "
                "connected_state_desc, synchronization_health_desc, "
                "recovery_health_desc}], 'total': <int>}."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.ha.fci",
        handler_attr="ha_fci",
        summary="List Failover Cluster Instance nodes + clustering flags.",
        description=(
            "Returns the WSFC nodes hosting the instance (sys.dm_os_cluster_nodes: "
            "node name, status, current owner) alongside the IsClustered flag "
            "and the server name. The migration-validation read for FCI "
            "topology. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "is_clustered": {"type": ["integer", "null"]},
                "server_name": {"type": ["string", "null"]},
            },
            "required": ["rows", "total"],
            "additionalProperties": True,
        },
        group_key="ha",
        tags=("read-only", "ha", "fci", "cluster", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to validate Failover Cluster Instance topology (which "
                "WSFC nodes host the instance, which owns it now, whether the "
                "instance is clustered at all). " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{node_name, status, status_description, "
                "is_current_owner}], 'total': <int>, 'is_clustered', "
                "'server_name'}."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.ha.sync-health",
        handler_attr="ha_sync_health",
        summary="Per-database AG replication sync health (queue depths, last commit).",
        description=(
            "Returns per-database replication state across AG replicas "
            "(sys.dm_hadr_database_replica_states): database name, replica "
            "server, is_local / is_primary_replica, synchronization state and "
            "health, suspended flag + reason, log-send and redo queue sizes, "
            "and last commit time. The fine-grained cutover-readiness read. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="ha",
        tags=("read-only", "ha", "sync-health", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to check per-database AG sync health (is every database "
                "SYNCHRONIZED and HEALTHY, how deep are the send/redo queues) "
                "before a cutover. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{database_name, replica_server_name, is_local, "
                "is_primary_replica, synchronization_state_desc, "
                "synchronization_health_desc, is_suspended, suspend_reason_desc, "
                "log_send_queue_size, redo_queue_size, last_commit_time}], "
                "'total': <int>}."
            ),
        },
    ),
)
