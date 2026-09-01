# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""T-SQL statements + row-shaping for the mssql connector (#3264).

Every function here builds one statement, runs it through the session
transport (:mod:`~meho_backplane.connectors.mssql.session`), and returns a
JSON-serialisable dict — the connector methods own nothing but the dispatch
glue, so this module stays a pure query surface (the postgres ``queries.py``
precedent, #2236). List reads return the ``{rows, total}`` envelope the
JSONFlux reducer keys on (it detects the single ``rows`` collection and
materialises it past the 50-row / 4 KB threshold); identity reads return a
flat dict.

Injection safety (see the :mod:`~meho_backplane.connectors.mssql.session`
docstring): operator-supplied **values** bind through ``pytds`` pyformat
placeholders (``%(name)s``); an operator-supplied **database identifier** in a
DDL / utility statement — which T-SQL cannot bind — is passed through
:func:`~meho_backplane.connectors.mssql.session.quote_identifier` (strict
pre-validation + QUOTENAME-style bracket escaping) before interpolation.

Catalog facts are pinned to the SQL Server 2022 system-view reference:
``sys.databases`` / ``sys.master_files`` / ``sys.configurations`` /
``sys.server_principals`` /
``sys.availability_groups`` + ``sys.dm_hadr_*`` / ``sys.dm_os_cluster_nodes`` /
``msdb.dbo.backupset``
(https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.mssql.session import (
    execute_statement,
    fetch_rows,
    quote_identifier,
)

__all__ = [
    "backup_database",
    "create_database",
    "drop_database",
    "fetch_availability_groups",
    "fetch_backup_history",
    "fetch_configurations",
    "fetch_database_files",
    "fetch_databases",
    "fetch_fci_nodes",
    "fetch_logins",
    "fetch_server_identity",
    "fetch_sync_health",
    "fetch_version",
    "restore_database",
]

#: Default cap for the ``mssql.backup.history`` read — the most recent N backup
#: rows, newest first. Bounded so a long-lived instance's backup history never
#: floods the transport before the reducer sees it.
_DEFAULT_BACKUP_HISTORY_LIMIT = 200


def _envelope(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap *rows* in the ``{rows, total}`` envelope the JSONFlux reducer keys on."""
    return {"rows": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# instance group
# ---------------------------------------------------------------------------

#: SERVERPROPERTY identity projection — the fingerprint canary. Constant script,
#: no operator input, so no injection surface.
_IDENTITY_SQL = (
    "SELECT "
    "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS product_version, "
    "CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)) AS product_level, "
    "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition, "
    "CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)) AS machine_name, "
    "CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)) AS server_name, "
    "CAST(SERVERPROPERTY('InstanceName') AS nvarchar(128)) AS instance_name, "
    "CAST(SERVERPROPERTY('Collation') AS nvarchar(128)) AS collation, "
    "CAST(SERVERPROPERTY('IsClustered') AS int) AS is_clustered, "
    "CAST(SERVERPROPERTY('IsHadrEnabled') AS int) AS is_hadr_enabled"
)


async def fetch_server_identity(target: Any, operator: Operator | None) -> dict[str, Any]:
    """One-row SERVERPROPERTY identity — product version / edition / clustering.

    Backs both :meth:`MssqlConnector.fingerprint` and the ``mssql.instance.about``
    op, so the canonical fingerprint and the operator-facing identity op share
    one round-trip.
    """
    rows = await fetch_rows(target, operator, _IDENTITY_SQL)
    return rows[0] if rows else {}


async def fetch_version(target: Any, operator: Operator | None) -> dict[str, Any]:
    """The full ``@@VERSION`` banner plus the parsed product version / level."""
    rows = await fetch_rows(
        target,
        operator,
        "SELECT @@VERSION AS version, "
        "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS product_version, "
        "CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)) AS product_level, "
        "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition",
    )
    return rows[0] if rows else {}


async def fetch_configurations(target: Any, operator: Operator | None) -> dict[str, Any]:
    """``sys.configurations`` — every instance-level setting (list-shaped)."""
    rows = await fetch_rows(
        target,
        operator,
        "SELECT name, value, value_in_use, minimum, maximum, "
        "is_dynamic, is_advanced, description "
        "FROM sys.configurations ORDER BY name",
    )
    return _envelope(rows)


async def fetch_logins(target: Any, operator: Operator | None) -> dict[str, Any]:
    """``sys.server_principals`` — server logins (list-shaped, no password material).

    Reads ``sys.server_principals`` (never ``sys.sql_logins``, which exposes
    ``password_hash``), so no credential material is ever projected. SQL /
    Windows / group / certificate / asymmetric-key principals only; the
    built-in fixed server roles (``type = 'R'``) are excluded.
    """
    rows = await fetch_rows(
        target,
        operator,
        "SELECT name, type_desc, is_disabled, create_date, modify_date, "
        "default_database_name "
        "FROM sys.server_principals "
        "WHERE type IN ('S', 'U', 'G', 'C', 'K') "
        "ORDER BY name",
    )
    return _envelope(rows)


# ---------------------------------------------------------------------------
# databases group
# ---------------------------------------------------------------------------

#: Per-database state + aggregated on-disk size. Sizes come from
#: ``sys.master_files`` (8 KB pages → MB), grouped so one row is one database.
_DATABASES_SQL = (
    "SELECT d.name, d.database_id, d.state_desc, d.recovery_model_desc, "
    "d.compatibility_level, d.collation_name, d.create_date, d.is_read_only, "
    "d.is_auto_close_on, d.containment_desc, "
    "CAST(SUM(mf.size) * 8.0 / 1024 AS decimal(18,2)) AS size_mb "
    "FROM sys.databases AS d "
    "LEFT JOIN sys.master_files AS mf ON mf.database_id = d.database_id "
    "GROUP BY d.name, d.database_id, d.state_desc, d.recovery_model_desc, "
    "d.compatibility_level, d.collation_name, d.create_date, d.is_read_only, "
    "d.is_auto_close_on, d.containment_desc "
    "ORDER BY size_mb DESC"
)


async def fetch_databases(target: Any, operator: Operator | None) -> dict[str, Any]:
    """``sys.databases`` — every database with state, recovery model, size."""
    rows = await fetch_rows(target, operator, _DATABASES_SQL)
    return _envelope(rows)


async def fetch_database_files(
    target: Any, operator: Operator | None, database: str | None
) -> dict[str, Any]:
    """``sys.master_files`` — data / log files, optionally scoped to one database.

    *database* is an optional filter bound as a **value** through the pyformat
    placeholder (``DB_NAME(database_id) = %(database)s``) — it is not an
    identifier here, so no bracket escaping applies; the bind is the injection
    defence.
    """
    sql = (
        "SELECT DB_NAME(mf.database_id) AS database_name, mf.file_id, "
        "mf.type_desc, mf.name AS logical_name, mf.physical_name, "
        "CAST(mf.size * 8.0 / 1024 AS decimal(18,2)) AS size_mb, "
        "mf.max_size, mf.growth, mf.is_percent_growth "
        "FROM sys.master_files AS mf"
    )
    params: dict[str, Any] = {}
    if database:
        sql += " WHERE DB_NAME(mf.database_id) = %(database)s"
        params["database"] = database
    sql += " ORDER BY database_name, mf.file_id"
    rows = await fetch_rows(target, operator, sql, params or None)
    return _envelope(rows)


async def create_database(target: Any, operator: Operator | None, database: str) -> dict[str, Any]:
    """``CREATE DATABASE`` — dangerous + approval-gated.

    The database name is an identifier T-SQL cannot bind, so it rides
    :func:`quote_identifier` (validated + bracket-escaped). Runs against
    ``master``; ``CREATE DATABASE`` cannot execute inside a multi-statement
    transaction, which the session's ``autocommit=True`` connection satisfies.
    """
    identifier = quote_identifier(database)
    await execute_statement(target, operator, f"CREATE DATABASE {identifier}", database=None)
    return {"database": database, "action": "create", "ok": True, "op_class": "write"}


async def drop_database(target: Any, operator: Operator | None, database: str) -> dict[str, Any]:
    """``DROP DATABASE`` — dangerous + approval-gated (irreversible)."""
    identifier = quote_identifier(database)
    await execute_statement(target, operator, f"DROP DATABASE {identifier}", database=None)
    return {"database": database, "action": "drop", "ok": True, "op_class": "write"}


# ---------------------------------------------------------------------------
# ha group — the migration-validation reads
# ---------------------------------------------------------------------------

#: Availability Group topology + per-replica role / sync state. One row per
#: replica: the join AG → replica → replica-state carries the operational and
#: synchronization health an operator validates before a migration cutover.
_AG_SQL = (
    "SELECT ag.name AS ag_name, ag.automated_backup_preference_desc, "
    "ag.failure_condition_level, ag.health_check_timeout, "
    "ar.replica_server_name, ar.availability_mode_desc, ar.failover_mode_desc, "
    "ars.role_desc, ars.operational_state_desc, ars.connected_state_desc, "
    "ars.synchronization_health_desc, ars.recovery_health_desc "
    "FROM sys.availability_groups AS ag "
    "JOIN sys.availability_replicas AS ar ON ar.group_id = ag.group_id "
    "LEFT JOIN sys.dm_hadr_availability_replica_states AS ars "
    "ON ars.replica_id = ar.replica_id "
    "ORDER BY ag.name, ar.replica_server_name"
)

#: Per-database replication sync health across AG replicas — the fine-grained
#: cutover-readiness read (queue depths + last commit time per database).
_SYNC_SQL = (
    "SELECT DB_NAME(drs.database_id) AS database_name, ar.replica_server_name, "
    "drs.is_local, drs.is_primary_replica, drs.synchronization_state_desc, "
    "drs.synchronization_health_desc, drs.is_suspended, drs.suspend_reason_desc, "
    "drs.log_send_queue_size, drs.redo_queue_size, drs.last_commit_time "
    "FROM sys.dm_hadr_database_replica_states AS drs "
    "LEFT JOIN sys.availability_replicas AS ar ON ar.replica_id = drs.replica_id "
    "ORDER BY database_name, ar.replica_server_name"
)


async def fetch_availability_groups(target: Any, operator: Operator | None) -> dict[str, Any]:
    """Availability Group topology + per-replica operational / sync state."""
    rows = await fetch_rows(target, operator, _AG_SQL)
    return _envelope(rows)


async def fetch_fci_nodes(target: Any, operator: Operator | None) -> dict[str, Any]:
    """Failover Cluster Instance topology — cluster nodes + clustering flags.

    ``sys.dm_os_cluster_nodes`` lists the WSFC nodes hosting the instance; the
    ``is_clustered`` / ``server_name`` scalars from SERVERPROPERTY ride
    alongside the ``{rows, total}`` node envelope (preserved as sibling scalars,
    not folded into the reduced collection).
    """
    nodes = await fetch_rows(
        target,
        operator,
        "SELECT NodeName AS node_name, status, status_description, is_current_owner "
        "FROM sys.dm_os_cluster_nodes ORDER BY NodeName",
    )
    identity = await fetch_rows(
        target,
        operator,
        "SELECT CAST(SERVERPROPERTY('IsClustered') AS int) AS is_clustered, "
        "CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)) AS server_name",
    )
    flags = identity[0] if identity else {}
    return {
        "rows": nodes,
        "total": len(nodes),
        "is_clustered": flags.get("is_clustered"),
        "server_name": flags.get("server_name"),
    }


async def fetch_sync_health(target: Any, operator: Operator | None) -> dict[str, Any]:
    """Per-database AG replication sync health (queue depths, last commit)."""
    rows = await fetch_rows(target, operator, _SYNC_SQL)
    return _envelope(rows)


# ---------------------------------------------------------------------------
# backup group
# ---------------------------------------------------------------------------


async def fetch_backup_history(
    target: Any,
    operator: Operator | None,
    database: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """``msdb.dbo.backupset`` — recent backup history, newest first (list-shaped).

    *database* (optional) and *limit* bind as **values** (``TOP (%(limit)s)`` /
    ``database_name = %(database)s``). ``type`` is the backup kind
    (``D`` full / ``I`` differential / ``L`` log / ``F`` file / ``G`` file-diff
    / ``P`` partial / ``Q`` partial-diff).
    """
    effective_limit = limit or _DEFAULT_BACKUP_HISTORY_LIMIT
    sql = (
        "SELECT TOP (%(limit)s) bs.database_name, bs.backup_start_date, "
        "bs.backup_finish_date, bs.type, bs.recovery_model, bs.is_copy_only, "
        "CAST(bs.backup_size AS decimal(20,0)) AS backup_size, "
        "CAST(bs.compressed_backup_size AS decimal(20,0)) AS compressed_backup_size, "
        # LSNs are numeric(25,0) identifiers, not quantities — cast to varchar so
        # their full 25-digit precision survives JSON (float/JS would truncate).
        "CAST(bs.first_lsn AS varchar(48)) AS first_lsn, "
        "CAST(bs.last_lsn AS varchar(48)) AS last_lsn, "
        "bs.server_name, bs.user_name "
        "FROM msdb.dbo.backupset AS bs"
    )
    params: dict[str, Any] = {"limit": int(effective_limit)}
    if database:
        sql += " WHERE bs.database_name = %(database)s"
        params["database"] = database
    sql += " ORDER BY bs.backup_finish_date DESC"
    rows = await fetch_rows(target, operator, sql, params)
    return _envelope(rows)


async def backup_database(
    target: Any, operator: Operator | None, database: str, path: str
) -> dict[str, Any]:
    """``BACKUP DATABASE ... TO DISK`` — caution (recoverable, creates a file).

    The database name rides :func:`quote_identifier` (identifier, not bindable);
    the destination *path* binds as a **value** (``TO DISK = %(path)s``) so an
    operator-supplied path can never inject T-SQL.
    """
    identifier = quote_identifier(database)
    await execute_statement(
        target,
        operator,
        f"BACKUP DATABASE {identifier} TO DISK = %(path)s WITH INIT",
        {"path": path},
        database=None,
    )
    return {
        "database": database,
        "action": "backup",
        "path": path,
        "ok": True,
        "op_class": "write",
    }


async def restore_database(
    target: Any,
    operator: Operator | None,
    database: str,
    path: str,
    replace: bool,
) -> dict[str, Any]:
    """``RESTORE DATABASE ... FROM DISK`` — dangerous + approval-gated (overwrites).

    Overwrites the target database, so the tier is ``dangerous`` +
    ``requires_approval`` (a dispatch parks for a human decision before the
    restore runs). The database name is bracket-escaped; the source *path*
    binds as a value. ``WITH REPLACE`` is appended only when *replace* is true.
    """
    identifier = quote_identifier(database)
    clause = " WITH REPLACE" if replace else ""
    await execute_statement(
        target,
        operator,
        f"RESTORE DATABASE {identifier} FROM DISK = %(path)s{clause}",
        {"path": path},
        database=None,
    )
    return {
        "database": database,
        "action": "restore",
        "path": path,
        "replace": replace,
        "ok": True,
        "op_class": "write",
    }
