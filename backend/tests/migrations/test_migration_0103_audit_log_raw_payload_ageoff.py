# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Migration ``0103``: the ``raw_payload`` age-off passes the append-only guard.

These tests run on a real PostgreSQL (testcontainers, the same
``pgvector/pgvector:pg16`` image as :mod:`tests.migrations.test_migration_rollback`),
because the ``audit_log_append_only`` trigger exists only on PostgreSQL. The
SQLite unit lane cannot see it, which is how the age-off and the guard
drifted apart in the first place.

What they prove:

(a) The real weekly tick (:func:`meho_backplane.audit_retention._run_one_prune_tick`,
    which calls ``_null_raw_payload_older_than``) empties ``raw_payload`` on
    an old row. The row stays, and every other column keeps its exact
    stored text.
(b) An ``UPDATE`` of any other column is rejected.
(c) An ``UPDATE`` that sets ``raw_payload`` to anything but SQL ``NULL``
    (or "empties" a row that is already empty) is rejected.
(d) An ``UPDATE`` that empties ``raw_payload`` **and** changes another
    column is rejected. This runs for every column of the table (read from
    the catalog, so a column added later is tested too), and for changes
    that keep the meaning but change the stored text (``json`` key order,
    spaces, a duplicate key, number format, SQL ``NULL`` vs JSON ``null``).
(e) ``DELETE`` is rejected.
(f) ``downgrade`` to ``0102`` puts back the exact strict ``0100`` function,
    and ``upgrade`` brings the age-off path back. If the new function would
    land in another schema (so the trigger keeps the strict one), the
    upgrade fails instead of reporting success.
(g) A row whose ``json`` holds text that ``jsonb`` cannot hold (a
    ``\\u0000`` or a lone ``\\ud800`` escape) is still emptied by the tick,
    and does not break the run.
(h) A role that does not own the table cannot change what the check does
    by putting its own functions or operators on its ``search_path``.

Rows are compared by the exact stored text of every column
(``column::text``), not through ``to_jsonb``: ``to_jsonb`` would hide
exactly the ``json`` changes that (d) must catch, and it fails on the rows
that (g) uses.

One container serves the whole module. Each test inserts its own rows and
checks only those rows. Only the tick tests insert rows older than the
retention window, and each tick empties them, so each tick's row count is
exact.

The tests are synchronous because ``alembic.command`` runs its own
:func:`asyncio.run` (see the env.py async cookbook); each database probe
runs inside its own ``asyncio.run`` boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import ColumnElement, insert, literal_column, null, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from meho_backplane import audit_retention
from meho_backplane.audit_retention import AUDIT_RAW_PAYLOAD_PRUNE_PATH
from meho_backplane.db.engine import dispose_engine, reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings


def _docker_socket_present() -> bool:
    """Same Docker heuristic as the other testcontainers-PG suites."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


pytestmark = pytest.mark.skipif(
    not _docker_socket_present(),
    reason=(
        "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
    ),
)

#: The exact error text the guard raises for a rejected write (format of 0100).
_REJECT_UPDATE = (
    "audit_log is append-only: UPDATE is not permitted (governance invariant, v0.1-spec section 6)"
)
_REJECT_DELETE = (
    "audit_log is append-only: DELETE is not permitted (governance invariant, v0.1-spec section 6)"
)

#: Placeholder bodies only. Nothing here looks like a real secret.
_RAW_BODY: dict[str, object] = {"pre_redaction": "raw-body-marker"}
_REDACTED_PAYLOAD: dict[str, object] = {
    "op_id": "test.op.read",
    "redaction_policy_id": "test-policy-v1",
}
_MANIFEST: list[dict[str, object]] = [
    {"rule": "r-test", "pattern": "test_pattern", "action": "redact", "count": 1}
]

#: Exact start texts for the "same meaning, other stored text" cases in (d).
_MANIFEST_TEXT = '[{"rule":"r","count":1}]'
_PAYLOAD_TEXT = '{"op_id":"test.op.read","n":1.0}'


def _async_url_from(sync_url: str) -> str:
    """Turn the testcontainers sync URL into the asyncpg URL the app uses."""
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@dataclass(frozen=True)
class _Pg:
    async_url: str
    #: ``pg_get_functiondef`` of ``audit_log_reject_mutation()`` as ``0100``
    #: created it (body and settings, such as a pinned ``search_path``).
    strict_function_def: str


def _alembic(async_url: str, action: str, revision: str) -> None:
    """Run ``alembic upgrade|downgrade <revision>`` against *async_url*.

    ``alembic/env.py`` prefers ``DATABASE_URL`` over the config value, and
    the autouse conftest fixture points ``DATABASE_URL`` at a per-test
    SQLite file. So both are set here, and ``DATABASE_URL`` is restored
    afterwards.
    """
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", async_url)
        getattr(command, action)(cfg, revision)


async def _guard_function(async_url: str) -> tuple[str, list[str] | None, bool]:
    """Return ``(pg_get_functiondef, proconfig, prosecdef)`` of the trigger's function."""
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT pg_get_functiondef(p.oid), p.proconfig, p.prosecdef "
                        "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                        "WHERE t.tgrelid = 'audit_log'::regclass "
                        "AND t.tgname = 'audit_log_append_only'"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    return str(row[0]), (list(row[1]) if row[1] is not None else None), bool(row[2])


@pytest.fixture(scope="module")
def pg() -> Iterator[_Pg]:
    """One PostgreSQL for the module: migrate to ``0102``, record the guard, then to head."""
    from testcontainers.postgres import PostgresContainer

    image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
    with PostgresContainer(image) as container:
        async_url = _async_url_from(container.get_connection_url())
        _alembic(async_url, "upgrade", "0102")
        strict_def, _, _ = asyncio.run(_guard_function(async_url))
        _alembic(async_url, "upgrade", "head")
        yield _Pg(async_url=async_url, strict_function_def=strict_def)


@pytest.fixture
def tick_env(pg: _Pg, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the app's settings and engine at the test PostgreSQL for one tick test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", pg.async_url)
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "90")
    get_settings.cache_clear()
    reset_engine_for_testing()
    try:
        yield
    finally:
        reset_engine_for_testing()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers (each runs inside one asyncio.run)
# ---------------------------------------------------------------------------


def _sql_value(value: str, sql_type: str) -> ColumnElement[Any]:
    """A SQL literal with this exact text, cast to *sql_type* (``json`` keeps the text).

    The ORM sends ``json`` columns through a ``jsonb`` cast, which would
    normalise the text, and refuses ``\\u0000``. A literal keeps the exact
    text. Only test constants go through here.
    """
    quoted = "'" + value.replace("'", "''") + "'"
    return literal_column(f"CAST({quoted} AS {sql_type})")


async def _insert_row(
    engine: AsyncEngine,
    *,
    occurred_at: datetime,
    raw_payload: object = _RAW_BODY,
    overrides: Mapping[str, object] | None = None,
) -> uuid.UUID:
    """Insert one fully populated audit row. ``raw_payload=None`` means SQL NULL.

    *overrides* replace column values; a value may be a SQL expression such
    as :func:`_sql_value` or :func:`sqlalchemy.null`.
    """
    row_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": row_id,
        "occurred_at": occurred_at,
        "operator_sub": "operator-0103",
        "method": "DISPATCH",
        "path": "test.op.read",
        "status_code": 200,
        "request_id": uuid.uuid4(),
        "duration_ms": Decimal("12.345"),
        "payload": dict(_REDACTED_PAYLOAD),
        "tenant_id": uuid.uuid4(),
        "target_id": uuid.uuid4(),
        "parent_audit_id": uuid.uuid4(),
        "agent_session_id": uuid.uuid4(),
        "actor_sub": "actor-0103",
        "raw_payload": null() if raw_payload is None else raw_payload,
        "redaction_manifest": list(_MANIFEST),
        "run_id": uuid.uuid4(),
        "step_id": "step-1",
        "work_ref": "work-0103",
        "policy_decision": "auto-execute",
    }
    values.update(overrides or {})
    async with engine.begin() as conn:
        await conn.execute(insert(AuditLog).values(**values))
    return row_id


async def _columns(conn: AsyncConnection) -> list[tuple[str, str]]:
    """Every column of ``audit_log`` as ``(name, type)``, in table order."""
    rows = await conn.execute(
        text(
            "SELECT attname, format_type(atttypid, NULL) FROM pg_attribute "
            "WHERE attrelid = 'audit_log'::regclass AND attnum > 0 AND NOT attisdropped "
            "ORDER BY attnum"
        )
    )
    return [(str(r[0]), str(r[1])) for r in rows]


async def _snapshot(engine: AsyncEngine, row_id: uuid.UUID) -> dict[str, str | None] | None:
    """Return every column's exact stored text (SQL NULL -> ``None``), or ``None`` if gone."""
    async with engine.connect() as conn:
        names = [name for name, _ in await _columns(conn)]
        select_list = ", ".join(f'a."{name}"::text' for name in names)
        row = (
            await conn.execute(
                text(f"SELECT {select_list} FROM audit_log a WHERE a.id = :id"),
                {"id": row_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return dict(zip(names, row, strict=True))


async def _try(
    engine: AsyncEngine,
    sql: str,
    row_id: uuid.UUID,
    params: Mapping[str, object] | None = None,
) -> str | None:
    """Run one statement in its own transaction; return the DB error text or ``None``."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), {"id": row_id, **(params or {})})
    except DBAPIError as exc:
        return str(exc.orig)
    return None


def _probe(
    async_url: str,
    sql: str,
    *,
    raw_payload: object = _RAW_BODY,
    overrides: Mapping[str, object] | None = None,
    params: Mapping[str, object] | None = None,
) -> tuple[str | None, dict[str, str | None] | None, dict[str, str | None] | None]:
    """Insert a recent row, run *sql* against it, return (error, before, after)."""

    async def _run() -> tuple[
        str | None, dict[str, str | None] | None, dict[str, str | None] | None
    ]:
        engine = create_async_engine(async_url)
        try:
            row_id = await _insert_row(
                engine,
                occurred_at=datetime.now(UTC),
                raw_payload=raw_payload,
                overrides=overrides,
            )
            before = await _snapshot(engine, row_id)
            error = await _try(engine, sql, row_id, params)
            after = await _snapshot(engine, row_id)
        finally:
            await engine.dispose()
        return error, before, after

    return asyncio.run(_run())


async def _tick(engine: AsyncEngine) -> list[dict[str, Any]]:
    """Run the real weekly tick once; return the payloads of the prune rows it wrote."""
    prune_rows = text("SELECT id, payload FROM audit_log WHERE path = :p")
    async with engine.connect() as conn:
        seen = {r[0] for r in await conn.execute(prune_rows, {"p": AUDIT_RAW_PAYLOAD_PRUNE_PATH})}

    # The real weekly tick, on the app's own engine (DATABASE_URL).
    await audit_retention._run_one_prune_tick()
    await dispose_engine()

    async with engine.connect() as conn:
        rows = (await conn.execute(prune_rows, {"p": AUDIT_RAW_PAYLOAD_PRUNE_PATH})).all()
    return [
        json.loads(payload) if isinstance(payload, str) else dict(payload)
        for row_id, payload in rows
        if row_id not in seen
    ]


def _without_raw(row: dict[str, str | None] | None) -> dict[str, str | None]:
    assert row is not None
    return {name: value for name, value in row.items() if name != "raw_payload"}


# ---------------------------------------------------------------------------
# (a) The real age-off tick works on PostgreSQL
# ---------------------------------------------------------------------------


def test_prune_tick_nulls_old_raw_payload_and_keeps_the_row(pg: _Pg, tick_env: None) -> None:
    async def _run() -> dict[str, Any]:
        engine = create_async_engine(pg.async_url)
        try:
            now = datetime.now(UTC)
            old_id = await _insert_row(engine, occurred_at=now - timedelta(days=120))
            recent_id = await _insert_row(engine, occurred_at=now)
            old_before = await _snapshot(engine, old_id)

            prune_payloads = await _tick(engine)
            # A second pass finds nothing left to empty.
            second_pass = await audit_retention._null_raw_payload_older_than(
                now - timedelta(days=90)
            )
            await dispose_engine()

            return {
                "old_before": old_before,
                "old_after": await _snapshot(engine, old_id),
                "recent_after": await _snapshot(engine, recent_id),
                "second_pass": second_pass,
                "prune_payloads": prune_payloads,
            }
        finally:
            await engine.dispose()

    state = asyncio.run(_run())

    old_before, old_after = state["old_before"], state["old_after"]
    assert old_before is not None and old_before["raw_payload"] is not None
    assert json.loads(old_before["raw_payload"]) == _RAW_BODY
    assert old_after is not None, "the age-off must keep the audit row"
    assert old_after["raw_payload"] is None, "raw_payload must be SQL NULL after the tick"
    assert _without_raw(old_after) == _without_raw(old_before), (
        "every other column must keep its exact stored text"
    )

    recent_after = state["recent_after"]
    assert recent_after is not None and recent_after["raw_payload"] is not None
    assert json.loads(recent_after["raw_payload"]) == _RAW_BODY, (
        "a row inside the retention window keeps its raw_payload"
    )
    assert state["second_pass"] == 0
    assert len(state["prune_payloads"]) == 1
    assert state["prune_payloads"][0]["nulled_raw_payload_rows"] == 1


# ---------------------------------------------------------------------------
# (b)-(e) Every other write is still rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "set_clause",
    [
        "status_code = 500",
        "operator_sub = 'someone-else'",
        "payload = '{}'::jsonb",
        "redaction_manifest = NULL",
        "occurred_at = occurred_at - interval '400 days'",
        "policy_decision = 'deny'",
        "status_code = status_code",
    ],
)
def test_update_of_another_column_is_rejected(pg: _Pg, set_clause: str) -> None:
    error, before, after = _probe(pg.async_url, f"UPDATE audit_log SET {set_clause} WHERE id = :id")
    assert error is not None and _REJECT_UPDATE in error
    assert after == before


@pytest.mark.parametrize(
    ("start_value", "set_clause"),
    [
        (_RAW_BODY, "raw_payload = '{\"other\": 1}'::jsonb"),
        (_RAW_BODY, "raw_payload = 'null'::jsonb"),
        (None, "raw_payload = '{\"refill\": 1}'::jsonb"),
        (None, "raw_payload = NULL"),
    ],
    ids=["value-to-other-value", "value-to-json-null", "null-to-value", "null-to-null"],
)
def test_update_raw_payload_to_anything_but_sql_null_is_rejected(
    pg: _Pg, start_value: object, set_clause: str
) -> None:
    error, before, after = _probe(
        pg.async_url,
        f"UPDATE audit_log SET {set_clause} WHERE id = :id",
        raw_payload=start_value,
    )
    assert error is not None and _REJECT_UPDATE in error
    assert after == before


#: How to change a column of each type, so a new value surely differs.
#: A type that is missing here makes the test below fail on purpose: add it.
_CHANGE_BY_TYPE: dict[str, str] = {
    "uuid": "gen_random_uuid()",
    "text": "COALESCE({col}, '') || '-changed'",
    "integer": "COALESCE({col}, 0) + 1",
    "numeric": "COALESCE({col}, 0) + 1",
    "timestamp with time zone": "COALESCE({col}, now()) + interval '1 second'",
    "jsonb": "'[\"changed\"]'::jsonb",
    "json": "'[\"changed\"]'::json",
}
#: Columns with a CHECK constraint get a value the constraint accepts, so
#: only the guard can reject the change.
_CHANGE_BY_COLUMN: dict[str, str] = {"policy_decision": "'deny'"}


def test_nulling_raw_payload_and_changing_any_other_column_is_rejected(pg: _Pg) -> None:
    """Every column of the table, read from the catalog: a new column is tested too."""

    async def _run() -> tuple[list[str], dict[str, str]]:
        engine = create_async_engine(pg.async_url)
        try:
            async with engine.connect() as conn:
                columns = [c for c in await _columns(conn) if c[0] != "raw_payload"]
            problems: dict[str, str] = {}
            for name, sql_type in columns:
                change = _CHANGE_BY_COLUMN.get(name) or _CHANGE_BY_TYPE.get(sql_type)
                if change is None:
                    problems[name] = f"no change expression for type {sql_type!r}"
                    continue
                expr = change.format(col=f'"{name}"')
                row_id = await _insert_row(engine, occurred_at=datetime.now(UTC))
                before = await _snapshot(engine, row_id)
                error = await _try(
                    engine,
                    f'UPDATE audit_log SET raw_payload = NULL, "{name}" = {expr} WHERE id = :id',
                    row_id,
                )
                after = await _snapshot(engine, row_id)
                if error is None or _REJECT_UPDATE not in error:
                    problems[name] = f"not rejected by the guard: {error!r}"
                elif after != before:
                    problems[name] = "row changed"
            return [name for name, _ in columns], problems
        finally:
            await engine.dispose()

    names, problems = asyncio.run(_run())
    # The catalog read really found the table (guards against an empty loop).
    assert {"id", "occurred_at", "status_code", "payload", "redaction_manifest"} <= set(names)
    assert problems == {}


@pytest.mark.parametrize(
    ("column", "sql_type", "start", "new"),
    [
        ("redaction_manifest", "json", _MANIFEST_TEXT, '[{"count":1,"rule":"r"}]'),
        ("redaction_manifest", "json", _MANIFEST_TEXT, '[ { "rule" : "r" , "count" : 1 } ]'),
        ("redaction_manifest", "json", _MANIFEST_TEXT, '[{"rule":"r","count":999,"count":1}]'),
        ("redaction_manifest", "json", _MANIFEST_TEXT, '[{"rule":"r","count":1.000}]'),
        ("redaction_manifest", "json", _MANIFEST_TEXT, '[{"rule":"r","count":1e0}]'),
        ("redaction_manifest", "json", _MANIFEST_TEXT, r'[{"rule":"\u0072","count":1}]'),
        ("redaction_manifest", "json", None, "null"),
        ("redaction_manifest", "json", "null", None),
        ("payload", "jsonb", _PAYLOAD_TEXT, '{"op_id":"test.op.read","n":1.00}'),
        ("payload", "jsonb", _PAYLOAD_TEXT, '{"op_id":"test.op.read","n":1}'),
    ],
    ids=[
        "manifest-key-order",
        "manifest-spaces",
        "manifest-duplicate-key",
        "manifest-number-1.000",
        "manifest-number-1e0",
        "manifest-unicode-escape",
        "manifest-sql-null-to-json-null",
        "manifest-json-null-to-sql-null",
        "payload-1.0-to-1.00",
        "payload-1.0-to-1",
    ],
)
def test_nulling_raw_payload_and_rewriting_a_value_in_another_form_is_rejected(
    pg: _Pg, column: str, sql_type: str, start: str | None, new: str | None
) -> None:
    """Values that compare equal as ``jsonb`` but are stored differently count as a change."""
    if start is not None and new is not None:

        async def _same_as_jsonb() -> bool:
            engine = create_async_engine(pg.async_url)
            try:
                async with engine.connect() as conn:
                    return bool(
                        (
                            await conn.execute(
                                text(
                                    "SELECT CAST(CAST(:a AS text) AS jsonb) "
                                    "= CAST(CAST(:b AS text) AS jsonb)"
                                ),
                                {"a": start, "b": new},
                            )
                        ).scalar_one()
                    )
            finally:
                await engine.dispose()

        # The case is real: as jsonb the two values are equal, as stored text not.
        assert start != new
        assert asyncio.run(_same_as_jsonb())

    start_value = null() if start is None else _sql_value(start, sql_type)
    new_sql = "NULL" if new is None else f"CAST(CAST(:v AS text) AS {sql_type})"
    error, before, after = _probe(
        pg.async_url,
        f"UPDATE audit_log SET raw_payload = NULL, {column} = {new_sql} WHERE id = :id",
        overrides={column: start_value},
        params={} if new is None else {"v": new},
    )
    assert error is not None and _REJECT_UPDATE in error
    assert after == before


@pytest.mark.parametrize("start_value", [_RAW_BODY, None], ids=["with-raw", "raw-already-null"])
def test_delete_is_rejected(pg: _Pg, start_value: object) -> None:
    error, before, after = _probe(
        pg.async_url, "DELETE FROM audit_log WHERE id = :id", raw_payload=start_value
    )
    assert error is not None and _REJECT_DELETE in error
    assert before is not None and after == before


@pytest.mark.parametrize(
    "set_clause",
    [
        "raw_payload = NULL",
        # Setting other columns to the value they already hold stores the
        # same bytes, so it is still only the age-off.
        "raw_payload = NULL, status_code = status_code, payload = payload, "
        "redaction_manifest = redaction_manifest, occurred_at = occurred_at",
    ],
    ids=["raw-only", "raw-plus-same-values"],
)
def test_nulling_only_raw_payload_is_allowed(pg: _Pg, set_clause: str) -> None:
    error, before, after = _probe(pg.async_url, f"UPDATE audit_log SET {set_clause} WHERE id = :id")
    assert error is None
    assert after is not None and after["raw_payload"] is None
    assert _without_raw(after) == _without_raw(before)


# ---------------------------------------------------------------------------
# (f) Downgrade restores the strict 0100 guard; upgrade brings 0103 back
# ---------------------------------------------------------------------------


def test_downgrade_restores_strict_guard_and_upgrade_reapplies(pg: _Pg) -> None:
    null_only = "UPDATE audit_log SET raw_payload = NULL WHERE id = :id"
    try:
        _alembic(pg.async_url, "downgrade", "0102")
        strict_def, strict_config, _ = asyncio.run(_guard_function(pg.async_url))
        assert strict_def == pg.strict_function_def, (
            "downgrade must restore the exact 0100 function (body and settings)"
        )
        assert strict_config is None, "the 0100 function has no pinned search_path"
        error, before, after = _probe(pg.async_url, null_only)
        assert error is not None and _REJECT_UPDATE in error, (
            "after downgrade even the raw_payload-only UPDATE is rejected again"
        )
        assert after == before
        error, _, _ = _probe(pg.async_url, "DELETE FROM audit_log WHERE id = :id")
        assert error is not None and _REJECT_DELETE in error
    finally:
        _alembic(pg.async_url, "upgrade", "head")

    head_def, _, _ = asyncio.run(_guard_function(pg.async_url))
    assert head_def != pg.strict_function_def
    error, _, after = _probe(pg.async_url, null_only)
    assert error is None and after is not None and after["raw_payload"] is None

    async def _trigger() -> list[tuple[str, str, str]]:
        engine = create_async_engine(pg.async_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT t.tgname, p.proname, t.tgenabled::text "
                        "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                        "WHERE t.tgrelid = 'audit_log'::regclass AND NOT t.tgisinternal"
                    )
                )
                return [(r[0], r[1], r[2]) for r in rows]
        finally:
            await engine.dispose()

    assert asyncio.run(_trigger()) == [
        ("audit_log_append_only", "audit_log_reject_mutation", "O")
    ], "the trigger keeps its name, its function, and stays enabled"


def test_upgrade_fails_when_the_trigger_would_keep_the_old_function(pg: _Pg) -> None:
    """The 0103 self-check: a function made in the wrong schema fails the migration.

    ``CREATE OR REPLACE FUNCTION`` has no schema name. A schema named after the
    migration role comes first on the default ``search_path`` (``"$user",
    public``). Without the self-check, 0103 would make a second, unused
    function there, report success, and leave the strict function on the
    trigger, so the age-off would keep failing every week.
    """

    async def _sql(statement: str) -> list[tuple[Any, ...]]:
        engine = create_async_engine(pg.async_url)
        try:
            async with engine.begin() as conn:
                result = await conn.exec_driver_sql(statement)
                return [tuple(row) for row in result.all()] if result.returns_rows else []
        finally:
            await engine.dispose()

    role_schema = asyncio.run(_sql("SELECT current_user"))[0][0]
    try:
        _alembic(pg.async_url, "downgrade", "0102")
        asyncio.run(_sql("CREATE SCHEMA AUTHORIZATION CURRENT_USER"))
        with pytest.raises(DBAPIError, match="migration 0103: trigger audit_log_append_only"):
            _alembic(pg.async_url, "upgrade", "head")

        # The whole upgrade is one transaction, so none of it stays behind.
        strict_def, _, _ = asyncio.run(_guard_function(pg.async_url))
        assert strict_def == pg.strict_function_def
        assert asyncio.run(_sql("SELECT version_num FROM alembic_version")) == [("0102",)]
        assert asyncio.run(
            _sql("SELECT count(*) FROM pg_proc WHERE proname = 'audit_log_reject_mutation'")
        ) == [(1,)]
    finally:
        asyncio.run(_sql(f'DROP SCHEMA IF EXISTS "{role_schema}" CASCADE'))
        # Back to 0102 and up again, so even a failed run here leaves the
        # real 0103 function on the trigger for the other tests.
        _alembic(pg.async_url, "downgrade", "0102")
        _alembic(pg.async_url, "upgrade", "head")

    _, config, _ = asyncio.run(_guard_function(pg.async_url))
    assert config == ["search_path=pg_catalog, pg_temp"]


# ---------------------------------------------------------------------------
# (g) json that jsonb cannot hold does not break the age-off
# ---------------------------------------------------------------------------


def test_prune_tick_empties_rows_whose_json_jsonb_cannot_hold(pg: _Pg, tick_env: None) -> None:
    """``\\u0000`` and a lone ``\\ud800`` are valid ``json`` but not valid ``jsonb``.

    A check that converts the row to ``jsonb`` fails on such a row, and the
    one weekly ``UPDATE`` fails with it. The byte compare never parses json.
    """
    escape_rows: dict[str, dict[str, object]] = {
        "raw-nul": {"raw_payload": _sql_value(r'{"name":"vm\u0000x"}', "json")},
        "raw-lone-surrogate": {"raw_payload": _sql_value(r'{"name":"\ud800"}', "json")},
        "manifest-nul": {
            "redaction_manifest": _sql_value(r'[{"rule":"r\u0000","count":1}]', "json")
        },
    }

    async def _run() -> dict[str, Any]:
        engine = create_async_engine(pg.async_url)
        try:
            old = datetime.now(UTC) - timedelta(days=120)
            ids = {
                label: await _insert_row(engine, occurred_at=old, overrides=overrides)
                for label, overrides in escape_rows.items()
            }
            # The data is what this test needs: to_jsonb fails on every row.
            jsonb_errors = {
                label: await _try(
                    engine, "SELECT to_jsonb(a) FROM audit_log a WHERE a.id = :id", row_id
                )
                for label, row_id in ids.items()
            }
            before = {label: await _snapshot(engine, row_id) for label, row_id in ids.items()}
            prune_payloads = await _tick(engine)
            after = {label: await _snapshot(engine, row_id) for label, row_id in ids.items()}
            return {
                "jsonb_errors": jsonb_errors,
                "before": before,
                "after": after,
                "prune_payloads": prune_payloads,
            }
        finally:
            await engine.dispose()

    state = asyncio.run(_run())

    assert all(error is not None for error in state["jsonb_errors"].values()), state["jsonb_errors"]
    for label in escape_rows:
        before, after = state["before"][label], state["after"][label]
        assert before is not None and before["raw_payload"] is not None
        assert after is not None, f"{label}: the row must stay"
        assert after["raw_payload"] is None, f"{label}: raw_payload must be emptied"
        assert _without_raw(after) == _without_raw(before), f"{label}: nothing else changes"
    assert len(state["prune_payloads"]) == 1
    assert state["prune_payloads"][0]["nulled_raw_payload_rows"] == len(escape_rows)


# ---------------------------------------------------------------------------
# (h) Names the caller controls cannot change the check
# ---------------------------------------------------------------------------

#: A role that may read, insert, update and delete audit rows, but does not
#: own the table, and owns a schema of its own.
_RW_ROLE = "audit_rw_0103"
_RW_SCHEMA = "rw_tools"

#: Ways to shadow a name the check could look up. Each one makes "is the row
#: unchanged?" answer yes if the check resolves the name in ``rw_tools``.
_SHADOW_TRICKS: dict[str, list[str]] = {
    "to_jsonb-exact-type": [
        "CREATE FUNCTION rw_tools.to_jsonb(audit_log) RETURNS jsonb "
        "LANGUAGE sql AS $f$ SELECT '{}'::jsonb $f$",
    ],
    "to_jsonb-any-type": [
        "CREATE FUNCTION rw_tools.to_jsonb(anyelement) RETURNS jsonb "
        "LANGUAGE sql AS $f$ SELECT '{}'::jsonb $f$",
    ],
    "text-equals": [
        "CREATE FUNCTION rw_tools.t_true(text, text) RETURNS boolean "
        "LANGUAGE sql AS $f$ SELECT true $f$",
        "CREATE OPERATOR rw_tools.= (LEFTARG = text, RIGHTARG = text, FUNCTION = rw_tools.t_true)",
    ],
    "jsonb-equals-and-minus": [
        "CREATE FUNCTION rw_tools.j_true(jsonb, jsonb) RETURNS boolean "
        "LANGUAGE sql AS $f$ SELECT true $f$",
        "CREATE OPERATOR rw_tools.= (LEFTARG = jsonb, RIGHTARG = jsonb, "
        "FUNCTION = rw_tools.j_true)",
        "CREATE FUNCTION rw_tools.j_minus(jsonb, text) RETURNS jsonb "
        "LANGUAGE sql AS $f$ SELECT '{}'::jsonb $f$",
        "CREATE OPERATOR rw_tools.- (LEFTARG = jsonb, RIGHTARG = text, "
        "FUNCTION = rw_tools.j_minus)",
    ],
    "row-compare": [
        "CREATE FUNCTION rw_tools.r_true(record, record) RETURNS boolean "
        "LANGUAGE plpgsql AS $f$ BEGIN RETURN true; END $f$",
        "CREATE OPERATOR rw_tools.*= (LEFTARG = record, RIGHTARG = record, "
        "FUNCTION = rw_tools.r_true)",
        "CREATE OPERATOR rw_tools.= (LEFTARG = record, RIGHTARG = record, "
        "FUNCTION = rw_tools.r_true)",
        "CREATE FUNCTION rw_tools.a_true(audit_log, audit_log) RETURNS boolean "
        "LANGUAGE sql AS $f$ SELECT true $f$",
        "CREATE OPERATOR rw_tools.*= (LEFTARG = audit_log, RIGHTARG = audit_log, "
        "FUNCTION = rw_tools.a_true)",
        "CREATE OPERATOR rw_tools.= (LEFTARG = audit_log, RIGHTARG = audit_log, "
        "FUNCTION = rw_tools.a_true)",
    ],
}
#: The caller's own schema after ``pg_catalog`` (the default place), and before it.
_HOSTILE_SEARCH_PATHS: dict[str, str] = {
    "schema-after-pg_catalog": '"$user", public, rw_tools',
    "schema-before-pg_catalog": "rw_tools, pg_catalog, public",
}


@pytest.fixture(scope="module")
def rw_role(pg: _Pg) -> str:
    """Create the non-owner role and its schema once for the module."""

    async def _setup() -> None:
        engine = create_async_engine(pg.async_url)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(f"CREATE ROLE {_RW_ROLE} NOLOGIN")
                await conn.exec_driver_sql(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON audit_log TO {_RW_ROLE}"
                )
                await conn.exec_driver_sql(f"CREATE SCHEMA {_RW_SCHEMA} AUTHORIZATION {_RW_ROLE}")
        finally:
            await engine.dispose()

    asyncio.run(_setup())
    return _RW_ROLE


def _as_rw_role(async_url: str, role: str, statements: list[str]) -> tuple[str | None, int]:
    """Run *statements* as *role* in one transaction, then roll it all back.

    Returns ``(error text or None, row count of the last statement)``.
    """

    async def _run() -> tuple[str | None, int]:
        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:
                trans = await conn.begin()
                try:
                    await conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
                    rowcount = 0
                    for stmt in statements:
                        result = await conn.exec_driver_sql(stmt)
                        rowcount = result.rowcount
                    return None, rowcount
                except DBAPIError as exc:
                    return str(exc.orig), 0
                finally:
                    await trans.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_guard_function_pins_search_path(pg: _Pg) -> None:
    """The general guard against every name trick, not only the ones below.

    With ``search_path = pg_catalog, pg_temp`` fixed on the function, every
    name the function looks up resolves in ``pg_catalog``, never in a schema
    the caller controls (``pg_temp`` is never searched for functions or
    operators). Not ``SECURITY DEFINER``, same as ``0100``.
    """
    _, config, security_definer = asyncio.run(_guard_function(pg.async_url))
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert security_definer is False


@pytest.mark.parametrize(
    "search_path", list(_HOSTILE_SEARCH_PATHS.values()), ids=list(_HOSTILE_SEARCH_PATHS)
)
@pytest.mark.parametrize("trick", list(_SHADOW_TRICKS))
def test_non_owner_cannot_forge_a_row_with_shadowed_names(
    pg: _Pg, rw_role: str, trick: str, search_path: str
) -> None:
    async def _row() -> tuple[uuid.UUID, dict[str, str | None] | None]:
        engine = create_async_engine(pg.async_url)
        try:
            row_id = await _insert_row(engine, occurred_at=datetime.now(UTC))
            return row_id, await _snapshot(engine, row_id)
        finally:
            await engine.dispose()

    row_id, before = asyncio.run(_row())
    where = f"WHERE id OPERATOR(pg_catalog.=) '{row_id}'::uuid"
    setup = [*_SHADOW_TRICKS[trick], f"SET LOCAL search_path = {search_path}"]

    # The forged write: empty raw_payload and rewrite the record of account.
    error, _ = _as_rw_role(
        pg.async_url,
        rw_role,
        [
            *setup,
            "UPDATE audit_log SET raw_payload = NULL, status_code = 401, "
            f"operator_sub = 'forged', payload = '{{}}'::jsonb {where}",
        ],
    )
    assert error is not None and _REJECT_UPDATE in error

    error, _ = _as_rw_role(pg.async_url, rw_role, [*setup, f"DELETE FROM audit_log {where}"])
    assert error is not None and _REJECT_DELETE in error

    # The real age-off still works for this role with the same names in place.
    error, rowcount = _as_rw_role(
        pg.async_url, rw_role, [*setup, f"UPDATE audit_log SET raw_payload = NULL {where}"]
    )
    assert error is None and rowcount == 1

    async def _after() -> dict[str, str | None] | None:
        engine = create_async_engine(pg.async_url)
        try:
            return await _snapshot(engine, row_id)
        finally:
            await engine.dispose()

    assert asyncio.run(_after()) == before
