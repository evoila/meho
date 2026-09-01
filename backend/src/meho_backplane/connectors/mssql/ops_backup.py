# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backup-group ops (history / database / restore) for :class:`MssqlConnector`.

Registered by :meth:`MssqlConnector.register_operations` via the
:data:`~meho_backplane.connectors.mssql.ops.MSSQL_OPS` composer. See
:mod:`meho_backplane.connectors.mssql.ops` for the ``MssqlOp`` dataclass and
the shared schema fragments.
"""

from __future__ import annotations

from meho_backplane.connectors.mssql.ops import (
    _BACKUP_PATH_PROP,
    _DATABASE_NAME_PROP,
    _LIST_RESPONSE_SCHEMA,
    TDS_TRANSPORT_NOTE,
    MssqlOp,
    _write_response_schema,
)

BACKUP_OPS: tuple[MssqlOp, ...] = (
    MssqlOp(
        op_id="mssql.backup.history",
        handler_attr="backup_history",
        summary="List recent backup history from msdb.dbo.backupset (newest first).",
        description=(
            "Lists recent backups (msdb.dbo.backupset): database, start/finish "
            "time, type (D full / I differential / L log / ...), recovery "
            "model, is_copy_only, backup size and compressed size, first/last "
            "LSN, server, user — newest first, capped at 'limit' (default 200). "
            "Pass 'database' to scope to one database. safety_level=safe, "
            "read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "database": _DATABASE_NAME_PROP,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "description": "Max backup rows to return (default 200, newest first).",
                },
            },
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="backup",
        tags=("read-only", "backup", "history", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read a database's recent backup history (when was the "
                "last full/log backup, how big, copy-only?) before a migration "
                "or a restore. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "database": "Optional. Scope to one database; omit for all.",
                "limit": "Optional. Row cap (default 200, max 10000).",
            },
            "output_shape": (
                "{'rows': [{database_name, backup_start_date, "
                "backup_finish_date, type, recovery_model, is_copy_only, "
                "backup_size, compressed_backup_size, first_lsn, last_lsn, "
                "server_name, user_name}], 'total': <int>}."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.backup.database",
        handler_attr="backup_database",
        summary="Back up a database to a disk device (caution).",
        description=(
            "Runs ``BACKUP DATABASE [<name>] TO DISK = <path> WITH INIT``. The "
            "database name is validated + bracket-escaped; the path binds as a "
            "value. safety_level=caution — a recoverable write (creates a backup "
            "file; overwrites the named device via WITH INIT), satellite-"
            "executable only through the Stage-3 composed write gate."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"database": _DATABASE_NAME_PROP, "path": _BACKUP_PATH_PROP},
            "required": ["database", "path"],
            "additionalProperties": False,
        },
        response_schema=_write_response_schema("path"),
        group_key="backup",
        tags=("write", "backup", "mssql"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Back up a database to a disk device the SQL Server service "
                "account can write. Recoverable; safety_level=caution. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "database": "Required. The database to back up.",
                "path": "Required. The backup device path (as the service account sees it).",
            },
            "output_shape": "{'database', 'action': 'backup', 'path', 'ok', 'op_class': 'write'}.",
        },
    ),
    MssqlOp(
        op_id="mssql.backup.restore",
        handler_attr="backup_restore",
        summary="Restore a database from a disk backup (dangerous, approval-gated).",
        description=(
            "Runs ``RESTORE DATABASE [<name>] FROM DISK = <path> [WITH REPLACE]`` "
            "— OVERWRITES the target database. The name is validated + "
            "bracket-escaped; the path binds as a value; WITH REPLACE is added "
            "only when 'replace' is true. safety_level=dangerous + "
            "requires_approval: a dispatch parks for a human decision, and the "
            "tier ladder excludes it from satellite runners."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "database": _DATABASE_NAME_PROP,
                "path": _BACKUP_PATH_PROP,
                "replace": {
                    "type": "boolean",
                    "description": "Add WITH REPLACE to overwrite an existing database.",
                },
            },
            "required": ["database", "path"],
            "additionalProperties": False,
        },
        response_schema=_write_response_schema("path", "replace"),
        group_key="backup",
        tags=("write", "restore", "mssql"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Restore a database from a backup device — OVERWRITES data. "
                "dangerous + requires approval — the dispatch parks for a human "
                "decision. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "database": "Required. The database to restore into.",
                "path": "Required. The backup device path to restore from.",
                "replace": "Optional. true to add WITH REPLACE (overwrite an existing database).",
            },
            "output_shape": (
                "{'database', 'action': 'restore', 'path', 'replace', 'ok', 'op_class': 'write'}."
            ),
        },
    ),
)
