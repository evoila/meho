# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SQL text + row-shaping for the read-only Postgres connector (#2236).

Every statement here is a catalog / statistics read against a
``default_transaction_read_only`` session. The functions take an already-open
:class:`asyncpg.Connection`, run one query, and return a JSON-serialisable
dict — the connector methods own the connect/close lifecycle so this module
stays a pure query surface (easy to read against the PostgreSQL monitoring
docs and to unit-test with a fake connection).

Catalog facts are pinned to the PostgreSQL 16 documentation:
``pg_stat_user_tables`` / ``pg_stat_user_indexes`` / ``pg_stat_activity``
(https://www.postgresql.org/docs/current/monitoring-stats.html) and the
``pg_database`` / ``pg_namespace`` / ``pg_settings`` system catalogs.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import uuid
from decimal import Decimal
from typing import Any

import asyncpg

__all__ = [
    "CURATED_SETTINGS",
    "fetch_activity",
    "fetch_databases",
    "fetch_fingerprint",
    "fetch_indexes",
    "fetch_schemas",
    "fetch_settings",
    "fetch_tables",
    "run_select",
]

#: The curated runtime settings ``postgres.settings`` returns when the caller
#: passes no explicit ``names`` filter — the parameters an operator reaches for
#: first when triaging connection pressure, memory, autovacuum, WAL, and
#: replication behaviour.
CURATED_SETTINGS: tuple[str, ...] = (
    "server_version",
    "max_connections",
    "superuser_reserved_connections",
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "maintenance_work_mem",
    "wal_level",
    "max_wal_size",
    "min_wal_size",
    "checkpoint_timeout",
    "autovacuum",
    "autovacuum_max_workers",
    "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_scale_factor",
    "statement_timeout",
    "idle_in_transaction_session_timeout",
    "default_transaction_read_only",
    "max_wal_senders",
    "hot_standby",
    "data_checksums",
    "ssl",
    "log_min_duration_statement",
)


def _jsonable(value: Any) -> Any:
    """Coerce an asyncpg scalar to a JSON-serialisable primitive.

    asyncpg maps PostgreSQL types to rich Python objects (``datetime``,
    ``Decimal``, ``UUID``, ``bytes``, ``ipaddress`` networks). The dispatcher
    wraps a handler's return value into an :class:`OperationResult` whose
    ``result`` must be JSON-serialisable, so this normaliser runs over every
    row value: temporals become ISO-8601 strings, ``Decimal`` becomes
    ``float``, and the remaining rich types become their canonical string.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(
        value,
        (
            uuid.UUID,
            ipaddress.IPv4Address,
            ipaddress.IPv6Address,
            ipaddress.IPv4Network,
            ipaddress.IPv6Network,
        ),
    ):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return str(value)


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    """Convert asyncpg records to JSON-serialisable dicts."""
    return [{key: _jsonable(val) for key, val in record.items()} for record in records]


_DATABASES_SQL = """
SELECT d.datname AS name,
       pg_catalog.pg_get_userbyid(d.datdba) AS owner,
       pg_catalog.pg_encoding_to_char(d.encoding) AS encoding,
       d.datcollate AS collate,
       d.datctype AS ctype,
       d.datallowconn AS allow_connections,
       pg_catalog.pg_database_size(d.datname) AS size_bytes
FROM pg_catalog.pg_database d
WHERE d.datistemplate = false
ORDER BY size_bytes DESC
"""

_SCHEMAS_SQL = """
SELECT n.nspname AS name,
       pg_catalog.pg_get_userbyid(n.nspowner) AS owner
FROM pg_catalog.pg_namespace n
WHERE n.nspname NOT LIKE 'pg\\_%'
  AND n.nspname <> 'information_schema'
ORDER BY n.nspname
"""

# Vacuum / analyze statistics + on-disk sizes per user table. Read from
# pg_stat_user_tables (the per-database activity view) joined implicitly with
# the size helpers keyed on the view's relid.
_TABLES_SQL = """
SELECT s.schemaname AS schema,
       s.relname AS name,
       s.n_live_tup AS live_tuples,
       s.n_dead_tup AS dead_tuples,
       s.n_mod_since_analyze AS mods_since_analyze,
       s.last_vacuum,
       s.last_autovacuum,
       s.last_analyze,
       s.last_autoanalyze,
       s.vacuum_count,
       s.autovacuum_count,
       s.analyze_count,
       s.autoanalyze_count,
       pg_catalog.pg_total_relation_size(s.relid) AS total_bytes,
       pg_catalog.pg_table_size(s.relid) AS table_bytes,
       pg_catalog.pg_indexes_size(s.relid) AS indexes_bytes
FROM pg_catalog.pg_stat_user_tables s
WHERE ($1::text IS NULL OR s.schemaname = $1)
ORDER BY total_bytes DESC
"""

# Index scan counters + on-disk size per user index.
_INDEXES_SQL = """
SELECT s.schemaname AS schema,
       s.relname AS table,
       s.indexrelname AS name,
       s.idx_scan AS scans,
       s.idx_tup_read AS tuples_read,
       s.idx_tup_fetch AS tuples_fetched,
       pg_catalog.pg_relation_size(s.indexrelid) AS size_bytes
FROM pg_catalog.pg_stat_user_indexes s
WHERE ($1::text IS NULL OR s.schemaname = $1)
ORDER BY size_bytes DESC
"""

# Session snapshot. The ``query`` text column is deliberately omitted: it can
# carry literal credential values from an in-flight statement, and this
# connector must never surface a secret. State + wait + timing columns give
# the operator what they need to triage blocking / idle-in-transaction.
_ACTIVITY_SQL = """
SELECT pid,
       datname AS database,
       usename AS username,
       application_name,
       client_addr::text AS client_addr,
       backend_type,
       state,
       wait_event_type,
       wait_event,
       backend_start,
       xact_start,
       query_start,
       state_change
FROM pg_catalog.pg_stat_activity
ORDER BY backend_start NULLS LAST
"""

_SETTINGS_SQL = """
SELECT name,
       setting,
       unit,
       category,
       short_desc,
       context,
       vartype,
       source,
       boot_val,
       reset_val,
       pending_restart
FROM pg_catalog.pg_settings
WHERE name = ANY($1::text[])
ORDER BY name
"""

_FINGERPRINT_SQL = """
SELECT current_setting('server_version') AS server_version,
       version() AS version_full,
       pg_is_in_recovery() AS in_recovery,
       current_setting('server_encoding') AS encoding,
       current_setting('data_checksums') AS data_checksums
"""

_DB_SIZES_SQL = """
SELECT datname AS name,
       pg_catalog.pg_database_size(datname) AS size_bytes
FROM pg_catalog.pg_database
WHERE datistemplate = false
ORDER BY size_bytes DESC
"""


async def fetch_databases(conn: asyncpg.Connection) -> dict[str, Any]:
    """List non-template databases with owner, encoding, and on-disk size."""
    records = await conn.fetch(_DATABASES_SQL)
    return {"databases": _rows(records)}


async def fetch_schemas(conn: asyncpg.Connection) -> dict[str, Any]:
    """List user schemas (system + ``information_schema`` excluded)."""
    records = await conn.fetch(_SCHEMAS_SQL)
    return {"schemas": _rows(records)}


async def fetch_tables(conn: asyncpg.Connection, schema: str | None) -> dict[str, Any]:
    """List user tables with vacuum/analyze statistics and on-disk sizes."""
    records = await conn.fetch(_TABLES_SQL, schema)
    return {"tables": _rows(records)}


async def fetch_indexes(conn: asyncpg.Connection, schema: str | None) -> dict[str, Any]:
    """List user indexes with scan counters and on-disk sizes."""
    records = await conn.fetch(_INDEXES_SQL, schema)
    return {"indexes": _rows(records)}


async def fetch_activity(conn: asyncpg.Connection) -> dict[str, Any]:
    """Snapshot ``pg_stat_activity`` (sessions), without the query text."""
    records = await conn.fetch(_ACTIVITY_SQL)
    return {"sessions": _rows(records)}


async def fetch_settings(conn: asyncpg.Connection, names: list[str] | None) -> dict[str, Any]:
    """Return curated (or caller-named) runtime settings from ``pg_settings``."""
    wanted = list(names) if names else list(CURATED_SETTINGS)
    records = await conn.fetch(_SETTINGS_SQL, wanted)
    return {"settings": _rows(records)}


async def run_select(conn: asyncpg.Connection, sql: str, max_rows: int) -> dict[str, Any]:
    """Run a pre-gated read-only SELECT and return up to *max_rows* rows.

    Fetches through a server-side cursor bounded to *max_rows* so an
    unbounded query result cannot exhaust memory; ``truncated`` flags whether
    more rows were available. The statement has already passed
    :func:`~meho_backplane.connectors.postgres.session.assert_read_only_sql`
    and runs on a ``default_transaction_read_only`` session, so it is doubly
    prevented from mutating state.
    """
    async with conn.transaction(readonly=True):
        cursor = await conn.cursor(sql)
        # Fetch one extra row to detect truncation without a second round-trip.
        records = await cursor.fetch(max_rows + 1)
    truncated = len(records) > max_rows
    rows = _rows(records[:max_rows])
    return {"rows": rows, "row_count": len(rows), "truncated": truncated}


async def fetch_fingerprint(conn: asyncpg.Connection) -> dict[str, Any]:
    """Return the canonical fingerprint fields plus per-database sizes."""
    row = await conn.fetchrow(_FINGERPRINT_SQL)
    sizes = await conn.fetch(_DB_SIZES_SQL)
    identity = {key: _jsonable(val) for key, val in row.items()} if row else {}
    identity["database_sizes"] = _rows(sizes)
    return identity
