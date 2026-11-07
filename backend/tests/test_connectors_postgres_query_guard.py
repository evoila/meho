# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the extra guards of the free-form ``postgres.query`` op.

* A statement that names a server admin, file, backup, replication, wait or
  remote-connection function is refused before a connection is opened, in
  any spelling (upper case, schema-qualified, double-quoted, inside a
  comment or a string, or as a Unicode-escaped identifier).
* A statement that names a built-in function that runs a SQL string
  (``query_to_xml``, ``ts_stat``, ``ts_rewrite``, ``crosstab``,
  ``connectby``) is refused too, so a denied name built from string pieces
  cannot reach the server through such a function.
* The query is refused when the login role is a superuser, or when the
  superuser flag cannot be read, unless the target sets
  ``extras["allow_superuser_query"] = true``.
* Every session carries a server-side ``statement_timeout``.

The wire is faked with a small connection double; the live counterpart runs
in :mod:`tests.integration.test_connectors_postgres_container`.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.postgres import (
    PostgresConnector,
    PostgresReadOnlyError,
    PostgresSuperuserError,
)
from meho_backplane.connectors.postgres import connector as connector_module
from meho_backplane.connectors.postgres import session as session_module
from meho_backplane.connectors.postgres.query_guard import (
    ALLOW_SUPERUSER_QUERY_EXTRA,
    DENIED_FUNCTION_PREFIXES,
    DENIED_FUNCTIONS,
    assert_no_admin_functions,
    assert_not_superuser,
    denied_function_in,
)
from meho_backplane.connectors.postgres.session import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    connect_read_only,
)


class _Target:
    def __init__(self, extras: dict[str, Any] | None = None) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
        self.name = "pg-guard"
        self.host = "pg.example.test"
        self.port = 5432
        self.secret_ref = None
        self.extras: dict[str, Any] = extras or {}


class _Cursor:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def fetch(self, n: int) -> list[dict[str, Any]]:
        return self._records[:n]


class _Txn:
    async def __aenter__(self) -> _Txn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    """Connection double: ``fetchval`` answers the superuser read."""

    def __init__(self, superuser: Any) -> None:
        self._superuser = superuser
        self.fetchval_sql: list[str] = []
        self.cursor_sql: str | None = None
        self.closed = False

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_sql.append(sql)
        return self._superuser

    def transaction(self, *, readonly: bool = False) -> _Txn:
        return _Txn()

    async def cursor(self, sql: str) -> _Cursor:
        self.cursor_sql = sql
        return _Cursor([{"n": 1}])

    async def close(self) -> None:
        self.closed = True


def _operator() -> Operator:
    return Operator(
        sub="op-guard",
        name=None,
        email=None,
        raw_jwt="op.guard.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000000c4"),
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_connection(monkeypatch: pytest.MonkeyPatch, conn: _Conn) -> AsyncMock:
    mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(connector_module, "connect_read_only", mock)
    return mock


# ---------------------------------------------------------------------------
# Admin function deny list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ("SELECT pg_terminate_backend(123)", "pg_terminate_backend"),
        ("SELECT pg_cancel_backend(123)", "pg_cancel_backend"),
        ("SELECT PG_RELOAD_CONF()", "pg_reload_conf"),
        ("SELECT pg_catalog.pg_rotate_logfile()", "pg_rotate_logfile"),
        ('SELECT "pg_terminate_backend"(1)', "pg_terminate_backend"),
        ("select pg_catalog . pg_read_file ( 'x' )", "pg_read_file"),
        ("SELECT pg_read_binary_file('x')", "pg_read_binary_file"),
        ("SELECT * FROM pg_ls_dir('.')", "pg_ls_dir"),
        ("SELECT * FROM pg_ls_waldir()", "pg_ls_waldir"),
        ("SELECT pg_stat_file('x')", "pg_stat_file"),
        ("SELECT lo_export(1, '/tmp/x')", "lo_export"),
        ("SELECT lo_import('/etc/x')", "lo_import"),
        ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
        ("SELECT pg_switch_wal()", "pg_switch_wal"),
        ("SELECT pg_backup_start('x')", "pg_backup_start"),
        ("SELECT pg_create_restore_point('x')", "pg_create_restore_point"),
        ("SELECT pg_promote()", "pg_promote"),
        ("SELECT pg_wal_replay_pause()", "pg_wal_replay_pause"),
        ("SELECT pg_drop_replication_slot('s')", "pg_drop_replication_slot"),
        (
            "SELECT pg_create_logical_replication_slot('s', 'p')",
            "pg_create_logical_replication_slot",
        ),
        (
            "SELECT * FROM pg_logical_slot_get_changes('s', NULL, NULL)",
            "pg_logical_slot_get_changes",
        ),
        ("SELECT pg_replication_origin_create('o')", "pg_replication_origin_create"),
        ("SELECT pg_stat_reset()", "pg_stat_reset"),
        ("SELECT pg_stat_reset_shared('bgwriter')", "pg_stat_reset_shared"),
        ("SELECT pg_stat_statements_reset()", "pg_stat_statements_reset"),
        ("SELECT pg_sleep(600)", "pg_sleep"),
        ("SELECT pg_sleep_for('10 minutes')", "pg_sleep_for"),
        ("SELECT pg_notify('c', 'p')", "pg_notify"),
        ("SELECT * FROM dblink('host=x', 'select 1') AS t(a int)", "dblink"),
        ("SELECT dblink_connect('c', 'host=x')", "dblink_connect"),
        ("SELECT postgres_fdw_disconnect_all()", "postgres_fdw_disconnect_all"),
        ("EXPLAIN ANALYZE SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
        ("WITH x AS (SELECT pg_reload_conf()) SELECT * FROM x", "pg_reload_conf"),
        # A name inside a comment or a string is refused too: the scan looks
        # at the raw text, so an unclosed quote cannot hide a later call.
        ("SELECT '/*', pg_terminate_backend(1) --*/", "pg_terminate_backend"),
        ("SELECT 1 /* pg_sleep */", "pg_sleep"),
    ],
)
def test_denied_function_is_found_in_any_spelling(sql: str, name: str) -> None:
    assert denied_function_in(sql) == name
    with pytest.raises(PostgresReadOnlyError, match=name):
        assert_no_admin_functions(sql)


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        # The server would join the pieces into a denied name and run it.
        (
            "SELECT query_to_xml('SELECT pg_' || 'terminate_backend(pg_backend_pid())', "
            "true, false, '')",
            "query_to_xml",
        ),
        ("SELECT QUERY_TO_XMLSCHEMA('SELECT 1', true, false, '')", "query_to_xmlschema"),
        (
            "SELECT pg_catalog.query_to_xml_and_xmlschema('SELECT 1', true, false, '')",
            "query_to_xml_and_xmlschema",
        ),
        ("SELECT cursor_to_xml('c', 10, true, false, '')", "cursor_to_xml"),
        ("SELECT cursor_to_xmlschema('c', true, false, '')", "cursor_to_xmlschema"),
        ("SELECT * FROM ts_stat('SELECT to_tsvector(pg_' || 'reload_conf()::text)')", "ts_stat"),
        ("SELECT ts_rewrite('a'::tsquery, 'SELECT t, s FROM aliases')", "ts_rewrite"),
        ("SELECT * FROM crosstab('SELECT 1, 2, 3') AS ct(a int, b int)", "crosstab"),
        ("SELECT * FROM crosstab2('SELECT 1, 2, 3')", "crosstab2"),
        ("SELECT * FROM crosstab_hash('SELECT 1', 'SELECT 2') AS ct(a int)", "crosstab_hash"),
        (
            "SELECT * FROM connectby('t', 'id', 'parent', '1', 0) AS c(id int, p int, l int)",
            "connectby",
        ),
    ],
)
def test_sql_running_function_is_refused(sql: str, name: str) -> None:
    """A built-in that runs a SQL string is refused before the wire.

    The string could carry a denied name built from pieces, which the word
    scan cannot see; the server would join and run it.
    """
    assert denied_function_in(sql) == name
    with pytest.raises(PostgresReadOnlyError, match=name):
        assert_no_admin_functions(sql)


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT U&"pg_\\0074erminate_backend"(1)',
        'SELECT u&"x" FROM t',
    ],
)
def test_unicode_escaped_identifier_is_refused(sql: str) -> None:
    with pytest.raises(PostgresReadOnlyError, match="Unicode-escaped"):
        assert_no_admin_functions(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT count(*) FROM orders",
        "SELECT current_setting('server_version')",
        "SELECT pg_backend_pid(), pg_is_in_recovery(), pg_current_wal_lsn()",
        "SELECT * FROM pg_stat_activity",
        "SELECT * FROM pg_file_settings",
        "SELECT * FROM pg_replication_origin_status",
        "SELECT rolname FROM pg_roles WHERE rolname = 'pg_read_all_data'",
        "SELECT pg_try_advisory_lock(1)",
        "SELECT pg_database_size('postgres')",
        "SHOW statement_timeout",
        "EXPLAIN SELECT * FROM orders",
        # Takes a table name, not SQL text; shares a stem with query_to_xml.
        "SELECT table_to_xml('orders', true, false, '')",
        "SELECT to_tsvector('english', 'a fat cat'), ts_headline('a fat cat', 'cat'::tsquery)",
    ],
)
def test_plain_reads_pass_the_deny_list(sql: str) -> None:
    assert denied_function_in(sql) is None
    assert_no_admin_functions(sql)


def test_deny_list_entries_are_lower_case_and_prefixes_do_not_cover_reads() -> None:
    assert all(name == name.lower() for name in DENIED_FUNCTIONS)
    assert all(prefix == prefix.lower() for prefix in DENIED_FUNCTION_PREFIXES)
    # Read-only catalog views and roles that share a stem with a denied
    # family must stay allowed.
    for name in ("pg_file_settings", "pg_replication_origin_status", "pg_read_all_data"):
        assert denied_function_in(f"SELECT * FROM {name}") is None, name


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ("SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
        (
            "SELECT query_to_xml('SELECT pg_' || 'terminate_backend(pg_backend_pid())', "
            "true, false, '')",
            "query_to_xml",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_query_refuses_admin_function_before_opening_connection(
    monkeypatch: pytest.MonkeyPatch, sql: str, name: str
) -> None:
    mock = _patch_connection(monkeypatch, _Conn(superuser=False))
    with pytest.raises(PostgresReadOnlyError, match=name):
        await PostgresConnector().run_query(_operator(), _Target(), {"sql": sql})
    mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Superuser refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_query_runs_for_a_role_that_is_not_a_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn(superuser=False)
    _patch_connection(monkeypatch, conn)
    result = await PostgresConnector().run_query(_operator(), _Target(), {"sql": "SELECT 1 AS n"})
    assert result["rows"] == [{"n": 1}]
    assert conn.fetchval_sql == [
        "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ]
    assert conn.cursor_sql == "SELECT 1 AS n"
    assert conn.closed is True


@pytest.mark.asyncio
async def test_run_query_refuses_a_superuser_login_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn(superuser=True)
    _patch_connection(monkeypatch, conn)
    with pytest.raises(PostgresSuperuserError, match="superuser") as excinfo:
        await PostgresConnector().run_query(_operator(), _Target(), {"sql": "SELECT 1"})
    assert ALLOW_SUPERUSER_QUERY_EXTRA in str(excinfo.value)
    assert conn.cursor_sql is None
    assert conn.closed is True


@pytest.mark.parametrize("answer", [None, 1, "on", "t"])
@pytest.mark.asyncio
async def test_run_query_refuses_when_the_superuser_flag_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    """Only a clear ``False`` lets the query run; anything else is a refusal."""
    conn = _Conn(superuser=answer)
    _patch_connection(monkeypatch, conn)
    with pytest.raises(PostgresSuperuserError, match="could not be read"):
        await PostgresConnector().run_query(_operator(), _Target(), {"sql": "SELECT 1"})
    assert conn.cursor_sql is None


@pytest.mark.asyncio
async def test_run_query_runs_as_superuser_when_the_target_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn(superuser=True)
    _patch_connection(monkeypatch, conn)
    target = _Target(extras={ALLOW_SUPERUSER_QUERY_EXTRA: True})
    result = await PostgresConnector().run_query(_operator(), target, {"sql": "SELECT 1 AS n"})
    assert result["row_count"] == 1
    assert conn.fetchval_sql == []  # the flag skips the superuser read


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
@pytest.mark.asyncio
async def test_target_flag_must_be_the_boolean_true(value: Any) -> None:
    conn = _Conn(superuser=True)
    with pytest.raises(PostgresSuperuserError):
        await assert_not_superuser(conn, _Target(extras={ALLOW_SUPERUSER_QUERY_EXTRA: value}))


@pytest.mark.asyncio
async def test_target_without_extras_mapping_is_checked() -> None:
    target = _Target()
    target.extras = None  # type: ignore[assignment]
    with pytest.raises(PostgresSuperuserError):
        await assert_not_superuser(_Conn(superuser=True), target)


# ---------------------------------------------------------------------------
# Statement timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_session_sets_a_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_connect = AsyncMock(return_value=_Conn(superuser=False))
    monkeypatch.setattr(session_module.asyncpg, "connect", fake_connect)

    await connect_read_only(_Target(), None)

    settings = fake_connect.await_args.kwargs["server_settings"]
    assert settings["statement_timeout"] == str(DEFAULT_STATEMENT_TIMEOUT_MS)
    assert int(settings["statement_timeout"]) > 0
