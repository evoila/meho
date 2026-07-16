# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated read ops exposed by :class:`PostgresConnector` (#2236).

The read core an operator needs to triage a self-hosted PostgreSQL instance
through the same dispatch -> policy-gate -> audit seam every other connector
uses, without reaching for ``psql`` against ``:5432``:

* ``postgres.databases`` -- non-template databases with owner, encoding, size.
* ``postgres.schemas`` -- user schemas in the connected database.
* ``postgres.tables`` -- user tables with vacuum/analyze stats + on-disk sizes.
* ``postgres.indexes`` -- user indexes with scan counters + on-disk sizes.
* ``postgres.activity`` -- current sessions (``pg_stat_activity``, no query text).
* ``postgres.settings`` -- curated (or caller-named) runtime settings.
* ``postgres.query`` -- a guarded free-form read-only SELECT.

Every op is ``safety_level="safe"`` + ``requires_approval=False`` and carries a
``read-only`` tag -- the connector registers no write/mutating op, and the
free-form ``postgres.query`` is doubly gated (first-keyword allowlist +
server-enforced ``default_transaction_read_only``). The dataclass + tuple shape
mirrors the loki (#2235) and bind9 (#367) siblings so the registration walk
reads identically.

Catalog + statistics facts are pinned to the PostgreSQL 16 monitoring docs
(https://www.postgresql.org/docs/current/monitoring-stats.html) and the
``pg_database`` / ``pg_namespace`` / ``pg_settings`` system-catalog reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["PG_OPS", "PG_WHEN_TO_USE_BY_GROUP", "PostgresOp"]


@dataclass(frozen=True)
class PostgresOp:
    """Metadata for one Postgres op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper.
    ``handler_attr`` is the async-handler attribute name on
    :class:`~meho_backplane.connectors.postgres.connector.PostgresConnector`.
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


#: Curated ``when_to_use`` blurbs per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set; the registrar
#: looks each op's ``group_key`` up here.
PG_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "postgres-inventory": (
        "Use to inventory a PostgreSQL instance's storage layout: list "
        "databases with their on-disk size (postgres.databases), the schemas "
        "in a database (postgres.schemas), the user tables with their "
        "vacuum/analyze statistics and total/table/index bytes "
        "(postgres.tables), or the user indexes with scan counts and size "
        "(postgres.indexes). The right group for 'why is this database so "
        "large?', 'which table is bloated / never vacuumed?', or 'which "
        "indexes are unused (idx_scan = 0)?'. Read-only."
    ),
    "postgres-runtime": (
        "Use to inspect a PostgreSQL instance's live runtime: the current "
        "sessions and what they are waiting on (postgres.activity, from "
        "pg_stat_activity) or the effective configuration parameters "
        "(postgres.settings). The right group for 'what is blocking / idle in "
        "transaction right now?' or 'what are max_connections / shared_buffers "
        "/ the autovacuum knobs set to?'. Read-only; the session query text is "
        "intentionally not returned by postgres.activity."
    ),
    "postgres-query": (
        "Use to run an ad-hoc read-only SQL query when no curated op fits "
        "(postgres.query). Only SELECT / SHOW / EXPLAIN / WITH / TABLE / VALUES "
        "statements are accepted -- the first keyword is allowlisted before "
        "execution and the session is server-enforced read-only, so a write "
        "is refused twice over. The right group for a custom catalog or "
        "statistics query the specific ops above don't cover."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: Optional target database. Catalog inventory (schemas/tables/indexes) is
#: per-database, so this scopes the connection; omit for the default
#: maintenance database.
_DATABASE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Database to connect to for this op. Schemas, tables, and indexes are "
        "per-database, so pass the database you want to inspect; omit to use "
        "the default maintenance database ('postgres')."
    ),
}

#: Optional schema filter for the table/index inventory ops.
_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "Restrict results to this schema (e.g. 'public'); omit for all user schemas.",
}

#: A generic list-shaped success envelope (the exact key varies per op).
_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


_DATABASES = PostgresOp(
    op_id="postgres.databases",
    handler_attr="list_databases",
    summary="List non-template databases with owner, encoding, and on-disk size.",
    description=(
        "Lists every non-template database in the cluster with its owner, "
        "character encoding, collation, whether connections are allowed, and "
        "its total on-disk size in bytes (pg_database_size), largest first. "
        "The starting point for 'why is this instance using so much disk?'. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE_SCHEMA,
    group_key="postgres-inventory",
    tags=("read-only", "postgres", "inventory", "databases"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first to see the databases on a Postgres instance and their "
            "sizes before drilling into schemas/tables of a specific one."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{databases:[{name, owner, encoding, collate, ctype, "
            "allow_connections, size_bytes}]} sorted by size_bytes desc."
        ),
    },
)


_SCHEMAS = PostgresOp(
    op_id="postgres.schemas",
    handler_attr="list_schemas",
    summary="List user schemas in a database.",
    description=(
        "Lists the user schemas in the connected database (system schemas and "
        "information_schema excluded) with their owner. Pass 'database' to "
        "inspect a database other than the default. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"database": _DATABASE_PROPERTY},
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE_SCHEMA,
    group_key="postgres-inventory",
    tags=("read-only", "postgres", "inventory", "schemas"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to enumerate the schemas in a database before listing its tables.",
        "parameter_hints": {"database": "Database to inspect; omit for the default."},
        "output_shape": "{schemas:[{name, owner}]}.",
    },
)


_TABLES = PostgresOp(
    op_id="postgres.tables",
    handler_attr="list_tables",
    summary="List user tables with vacuum/analyze statistics and on-disk sizes.",
    description=(
        "Lists user tables from pg_stat_user_tables with their live/dead tuple "
        "counts, modifications since the last analyze, the last (auto)vacuum "
        "and (auto)analyze timestamps and counts, and total/table/index sizes "
        "in bytes, largest first. The op for 'which table is bloated "
        "(high dead_tuples) or has never been vacuumed?'. Pass 'database' to "
        "target a specific database and 'schema' to restrict the results. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"database": _DATABASE_PROPERTY, "schema": _SCHEMA_PROPERTY},
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE_SCHEMA,
    group_key="postgres-inventory",
    tags=("read-only", "postgres", "inventory", "tables", "vacuum"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to find bloated or un-vacuumed tables, or to see which tables "
            "dominate a database's storage."
        ),
        "parameter_hints": {
            "database": "Database to inspect; omit for the default.",
            "schema": "Restrict to one schema (e.g. 'public'); omit for all.",
        },
        "output_shape": (
            "{tables:[{schema, name, live_tuples, dead_tuples, "
            "mods_since_analyze, last_vacuum, last_autovacuum, last_analyze, "
            "last_autoanalyze, vacuum_count, autovacuum_count, analyze_count, "
            "autoanalyze_count, total_bytes, table_bytes, indexes_bytes}]} "
            "sorted by total_bytes desc. Timestamps are ISO-8601 or null."
        ),
    },
)


_INDEXES = PostgresOp(
    op_id="postgres.indexes",
    handler_attr="list_indexes",
    summary="List user indexes with scan counters and on-disk sizes.",
    description=(
        "Lists user indexes from pg_stat_user_indexes with their scan count "
        "(idx_scan), tuples read/fetched, and on-disk size in bytes, largest "
        "first. The op for 'which indexes are never used (idx_scan = 0) and "
        "waste space?'. Pass 'database' to target a specific database and "
        "'schema' to restrict the results. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"database": _DATABASE_PROPERTY, "schema": _SCHEMA_PROPERTY},
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE_SCHEMA,
    group_key="postgres-inventory",
    tags=("read-only", "postgres", "inventory", "indexes"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to find unused indexes (idx_scan = 0) or the largest indexes.",
        "parameter_hints": {
            "database": "Database to inspect; omit for the default.",
            "schema": "Restrict to one schema; omit for all.",
        },
        "output_shape": (
            "{indexes:[{schema, table, name, scans, tuples_read, "
            "tuples_fetched, size_bytes}]} sorted by size_bytes desc."
        ),
    },
)


_ACTIVITY = PostgresOp(
    op_id="postgres.activity",
    handler_attr="activity",
    summary="Snapshot the current sessions from pg_stat_activity.",
    description=(
        "Returns the current backend sessions from pg_stat_activity: pid, "
        "database, user, application name, client address, backend type, "
        "state, wait_event_type/wait_event, and the backend/xact/query/state "
        "timestamps. The op for 'what is running / blocking / idle in "
        "transaction right now?'. The in-flight query TEXT is intentionally "
        "omitted because it can contain literal secrets. safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE_SCHEMA,
    group_key="postgres-runtime",
    tags=("read-only", "postgres", "runtime", "activity"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to triage live activity: long-running or idle-in-transaction "
            "sessions, lock waits, or connection pressure."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{sessions:[{pid, database, username, application_name, "
            "client_addr, backend_type, state, wait_event_type, wait_event, "
            "backend_start, xact_start, query_start, state_change}]}. No query "
            "text is returned."
        ),
    },
)


_SETTINGS = PostgresOp(
    op_id="postgres.settings",
    handler_attr="settings",
    summary="Return curated (or caller-named) runtime configuration settings.",
    description=(
        "Returns runtime configuration parameters from pg_settings. With no "
        "'names' filter it returns a curated set covering connections, memory, "
        "WAL, autovacuum, timeouts, checksums, and replication; pass 'names' "
        "to fetch a specific list. Each entry carries the effective setting, "
        "unit, category, description, context, source, boot/reset values, and "
        "whether a restart is pending. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "description": (
                    "Specific setting names to fetch (e.g. ['shared_buffers', "
                    "'work_mem']); omit for the curated default set."
                ),
            }
        },
        "additionalProperties": False,
    },
    response_schema=_LIST_RESPONSE_SCHEMA,
    group_key="postgres-runtime",
    tags=("read-only", "postgres", "runtime", "settings"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to read effective configuration (memory, connections, "
            "autovacuum, WAL). Pass 'names' for specific parameters."
        ),
        "parameter_hints": {
            "names": "Optional list of pg_settings names; omit for the curated set."
        },
        "output_shape": (
            "{settings:[{name, setting, unit, category, short_desc, context, "
            "vartype, source, boot_val, reset_val, pending_restart}]}."
        ),
    },
)


_QUERY = PostgresOp(
    op_id="postgres.query",
    handler_attr="run_query",
    summary="Run a guarded free-form read-only SQL query.",
    description=(
        "Runs an ad-hoc read-only SQL statement. The first keyword must be one "
        "of SELECT / SHOW / EXPLAIN / WITH / TABLE / VALUES -- it is "
        "allowlisted before execution -- and the session is server-enforced "
        "read-only (default_transaction_read_only=on), so a mutating statement "
        "is refused twice over. Results are capped at 'max_rows' (default 1000) "
        "with a 'truncated' flag. Pass 'database' to target a specific "
        "database. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": (
                    "The read-only SQL statement (must begin with "
                    "SELECT/SHOW/EXPLAIN/WITH/TABLE/VALUES)."
                ),
            },
            "database": _DATABASE_PROPERTY,
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10000,
                "description": "Maximum rows to return (default 1000).",
            },
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {"type": "array"},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
    group_key="postgres-query",
    tags=("read-only", "postgres", "query", "sql"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call only when no curated op fits. The statement must be a "
            "read-only SELECT/SHOW/EXPLAIN/WITH/TABLE/VALUES; writes are "
            "rejected before execution and by the server."
        ),
        "parameter_hints": {
            "sql": "A read-only SQL statement.",
            "database": "Database to run against; omit for the default.",
            "max_rows": "Row cap (default 1000, max 10000).",
        },
        "output_shape": "{rows:[{...}], row_count, truncated}.",
    },
)


#: The ops :class:`PostgresConnector` registers at lifespan startup.
PG_OPS: tuple[PostgresOp, ...] = (
    _DATABASES,
    _SCHEMAS,
    _TABLES,
    _INDEXES,
    _ACTIVITY,
    _SETTINGS,
    _QUERY,
)
