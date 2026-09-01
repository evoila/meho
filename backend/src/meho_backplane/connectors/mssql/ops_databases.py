# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Database-group ops (list / files / create / drop) for :class:`MssqlConnector`.

Registered by :meth:`MssqlConnector.register_operations` via the
:data:`~meho_backplane.connectors.mssql.ops.MSSQL_OPS` composer. See
:mod:`meho_backplane.connectors.mssql.ops` for the ``MssqlOp`` dataclass and
the shared schema fragments.
"""

from __future__ import annotations

from meho_backplane.connectors.mssql.ops import (
    _DATABASE_NAME_PROP,
    _LIST_RESPONSE_SCHEMA,
    _NO_PARAMS,
    TDS_TRANSPORT_NOTE,
    MssqlOp,
    _write_response_schema,
)

DATABASE_OPS: tuple[MssqlOp, ...] = (
    MssqlOp(
        op_id="mssql.databases.list",
        handler_attr="databases_list",
        summary="List databases with state, recovery model, and on-disk size.",
        description=(
            "Lists every database (sys.databases) with its state, recovery "
            "model, compatibility level, collation, create date, read-only / "
            "auto-close / containment flags, and aggregated on-disk size in MB "
            "(from sys.master_files), largest first. safety_level=safe, "
            "read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="databases",
        tags=("read-only", "databases", "inventory", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to inventory the databases on an instance with their "
                "state and size before drilling into files or planning a "
                "migration. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{name, database_id, state_desc, "
                "recovery_model_desc, compatibility_level, collation_name, "
                "create_date, is_read_only, is_auto_close_on, containment_desc, "
                "size_mb}], 'total': <int>} sorted by size_mb desc."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.databases.files",
        handler_attr="databases_files",
        summary="List database data/log files (optionally scoped to one database).",
        description=(
            "Lists data and log files (sys.master_files) with database name, "
            "file id, type, logical name, physical path, size in MB, max size, "
            "and growth. Pass 'database' to scope to one database; omit for all. "
            "safety_level=safe, read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"database": _DATABASE_NAME_PROP},
            "additionalProperties": False,
        },
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="databases",
        tags=("read-only", "databases", "files", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to see the physical files behind a database (paths, "
                "sizes, growth) — e.g. to plan storage before a migration. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {"database": "Optional. Scope to one database; omit for all."},
            "output_shape": (
                "{'rows': [{database_name, file_id, type_desc, logical_name, "
                "physical_name, size_mb, max_size, growth, is_percent_growth}], "
                "'total': <int>}."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.databases.create",
        handler_attr="databases_create",
        summary="Create a database via CREATE DATABASE (dangerous, approval-gated).",
        description=(
            "Runs ``CREATE DATABASE [<name>]`` against the instance. The name is "
            "validated and bracket-escaped (QUOTENAME-style) — never "
            "string-interpolated raw. safety_level=dangerous + "
            "requires_approval: a dispatch parks for a human decision before "
            "anything is created, and the tier ladder excludes it from "
            "satellite runners."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"database": _DATABASE_NAME_PROP},
            "required": ["database"],
            "additionalProperties": False,
        },
        response_schema=_write_response_schema(),
        group_key="databases",
        tags=("write", "databases", "create", "mssql"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Create a new database. dangerous + requires approval — the "
                "dispatch parks for a human decision. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {"database": "Required. The database name to create."},
            "output_shape": "{'database', 'action': 'create', 'ok', 'op_class': 'write'}.",
        },
    ),
    MssqlOp(
        op_id="mssql.databases.drop",
        handler_attr="databases_drop",
        summary="Drop a database via DROP DATABASE (dangerous, approval-gated).",
        description=(
            "Runs ``DROP DATABASE [<name>]`` — irreversible removal of the "
            "database and its files. The name is validated and bracket-escaped. "
            "safety_level=dangerous + requires_approval: a dispatch parks for a "
            "human decision, and the tier ladder excludes it from satellite "
            "runners."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"database": _DATABASE_NAME_PROP},
            "required": ["database"],
            "additionalProperties": False,
        },
        response_schema=_write_response_schema(),
        group_key="databases",
        tags=("write", "databases", "drop", "mssql"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Drop (delete) a database. Irreversible; dangerous + requires "
                "approval — the dispatch parks for a human decision. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {"database": "Required. The database name to drop."},
            "output_shape": "{'database', 'action': 'drop', 'ok', 'op_class': 'write'}.",
        },
    ),
)
