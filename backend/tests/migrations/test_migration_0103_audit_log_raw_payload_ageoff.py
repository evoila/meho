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
    an old row. The row stays, and every other column is unchanged.
(b) An ``UPDATE`` of any other column is rejected.
(c) An ``UPDATE`` that sets ``raw_payload`` to anything but SQL ``NULL``
    (or "empties" a row that is already empty) is rejected.
(d) An ``UPDATE`` that empties ``raw_payload`` **and** changes another
    column is rejected.
(e) ``DELETE`` is rejected.
(f) ``downgrade`` to ``0102`` puts back the exact strict ``0100`` function,
    and ``upgrade`` brings the age-off path back.

One container serves the whole module. Each test inserts its own rows and
checks only those rows. Only test (a) inserts rows older than the
retention window, so the tick's row count is exact.

The tests are synchronous because ``alembic.command`` runs its own
:func:`asyncio.run` (see the env.py async cookbook); each database probe
runs inside its own ``asyncio.run`` boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import insert, null, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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


def _async_url_from(sync_url: str) -> str:
    """Turn the testcontainers sync URL into the asyncpg URL the app uses."""
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


@dataclass(frozen=True)
class _Pg:
    async_url: str
    #: ``prosrc`` of ``audit_log_reject_mutation()`` as ``0100`` created it.
    strict_function_src: str


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


async def _function_src(async_url: str) -> str:
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            src = (
                await conn.execute(
                    text("SELECT prosrc FROM pg_proc WHERE proname = 'audit_log_reject_mutation'")
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return str(src)


@pytest.fixture(scope="module")
def pg() -> Iterator[_Pg]:
    """One PostgreSQL for the module: migrate to ``0102``, record the guard, then to head."""
    from testcontainers.postgres import PostgresContainer

    image = os.environ.get("MEHO_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
    with PostgresContainer(image) as container:
        async_url = _async_url_from(container.get_connection_url())
        _alembic(async_url, "upgrade", "0102")
        strict_src = asyncio.run(_function_src(async_url))
        _alembic(async_url, "upgrade", "head")
        yield _Pg(async_url=async_url, strict_function_src=strict_src)


# ---------------------------------------------------------------------------
# Helpers (each runs inside one asyncio.run)
# ---------------------------------------------------------------------------


async def _insert_row(
    engine: AsyncEngine,
    *,
    occurred_at: datetime,
    raw_payload: object,
) -> uuid.UUID:
    """Insert one fully populated audit row. ``raw_payload=None`` means SQL NULL."""
    row_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            insert(AuditLog).values(
                id=row_id,
                occurred_at=occurred_at,
                operator_sub="operator-0103",
                method="DISPATCH",
                path="test.op.read",
                status_code=200,
                request_id=uuid.uuid4(),
                duration_ms=Decimal("12.345"),
                payload=dict(_REDACTED_PAYLOAD),
                tenant_id=uuid.uuid4(),
                target_id=uuid.uuid4(),
                parent_audit_id=uuid.uuid4(),
                agent_session_id=uuid.uuid4(),
                actor_sub="actor-0103",
                raw_payload=null() if raw_payload is None else raw_payload,
                redaction_manifest=list(_MANIFEST),
                run_id=uuid.uuid4(),
                step_id="step-1",
                work_ref="work-0103",
                policy_decision="auto-execute",
            )
        )
    return row_id


async def _snapshot(engine: AsyncEngine, row_id: uuid.UUID) -> dict[str, Any] | None:
    """Return the whole row as a dict (via ``to_jsonb``), or ``None`` if gone."""
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT to_jsonb(a) FROM audit_log a WHERE a.id = :id"),
                {"id": row_id},
            )
        ).scalar_one_or_none()
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else dict(value)


async def _try(engine: AsyncEngine, sql: str, row_id: uuid.UUID) -> str | None:
    """Run one statement in its own transaction; return the DB error text or ``None``."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), {"id": row_id})
    except DBAPIError as exc:
        return str(exc.orig)
    return None


def _probe(
    async_url: str, sql: str, *, raw_payload: object = _RAW_BODY
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Insert a recent row, run *sql* against it, return (error, before, after)."""

    async def _run() -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
        engine = create_async_engine(async_url)
        try:
            row_id = await _insert_row(
                engine, occurred_at=datetime.now(UTC), raw_payload=raw_payload
            )
            before = await _snapshot(engine, row_id)
            error = await _try(engine, sql, row_id)
            after = await _snapshot(engine, row_id)
        finally:
            await engine.dispose()
        return error, before, after

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# (a) The real age-off tick works on PostgreSQL
# ---------------------------------------------------------------------------


def test_prune_tick_nulls_old_raw_payload_and_keeps_the_row(
    pg: _Pg, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", pg.async_url)
    monkeypatch.setenv("RAW_PAYLOAD_RETENTION_DAYS", "90")
    get_settings.cache_clear()
    reset_engine_for_testing()

    async def _run() -> dict[str, Any]:
        engine = create_async_engine(pg.async_url)
        try:
            now = datetime.now(UTC)
            old_id = await _insert_row(
                engine, occurred_at=now - timedelta(days=120), raw_payload=_RAW_BODY
            )
            recent_id = await _insert_row(engine, occurred_at=now, raw_payload=_RAW_BODY)
            old_before = await _snapshot(engine, old_id)

            # The real weekly tick, on the app's own engine (DATABASE_URL).
            await audit_retention._run_one_prune_tick()
            # A second pass finds nothing left to empty.
            second_pass = await audit_retention._null_raw_payload_older_than(
                now - timedelta(days=90)
            )
            await dispose_engine()

            async with engine.connect() as conn:
                prune_payloads = (
                    (
                        await conn.execute(
                            text("SELECT payload FROM audit_log WHERE path = :p"),
                            {"p": AUDIT_RAW_PAYLOAD_PRUNE_PATH},
                        )
                    )
                    .scalars()
                    .all()
                )
            return {
                "old_before": old_before,
                "old_after": await _snapshot(engine, old_id),
                "recent_after": await _snapshot(engine, recent_id),
                "second_pass": second_pass,
                "prune_payloads": [
                    json.loads(p) if isinstance(p, str) else p for p in prune_payloads
                ],
            }
        finally:
            await engine.dispose()

    try:
        state = asyncio.run(_run())
    finally:
        reset_engine_for_testing()
        get_settings.cache_clear()

    old_before, old_after = state["old_before"], state["old_after"]
    assert old_before is not None and old_before["raw_payload"] == _RAW_BODY
    assert old_after is not None, "the age-off must keep the audit row"
    assert old_after["raw_payload"] is None, "raw_payload must be SQL NULL after the tick"
    old_before.pop("raw_payload")
    old_after.pop("raw_payload")
    assert old_after == old_before, "every other column must stay exactly the same"

    recent_after = state["recent_after"]
    assert recent_after is not None and recent_after["raw_payload"] == _RAW_BODY, (
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


@pytest.mark.parametrize(
    "extra",
    [
        "status_code = 500",
        "payload = '{}'::jsonb",
        "occurred_at = occurred_at - interval '400 days'",
    ],
)
def test_nulling_raw_payload_together_with_another_column_is_rejected(pg: _Pg, extra: str) -> None:
    error, before, after = _probe(
        pg.async_url, f"UPDATE audit_log SET raw_payload = NULL, {extra} WHERE id = :id"
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


def test_nulling_only_raw_payload_is_allowed(pg: _Pg) -> None:
    error, before, after = _probe(
        pg.async_url, "UPDATE audit_log SET raw_payload = NULL WHERE id = :id"
    )
    assert error is None
    assert before is not None and after is not None
    assert after["raw_payload"] is None
    before.pop("raw_payload")
    after.pop("raw_payload")
    assert after == before


# ---------------------------------------------------------------------------
# (f) Downgrade restores the strict 0100 guard; upgrade brings 0103 back
# ---------------------------------------------------------------------------


def test_downgrade_restores_strict_guard_and_upgrade_reapplies(pg: _Pg) -> None:
    null_only = "UPDATE audit_log SET raw_payload = NULL WHERE id = :id"
    try:
        _alembic(pg.async_url, "downgrade", "0102")
        assert asyncio.run(_function_src(pg.async_url)) == pg.strict_function_src, (
            "downgrade must restore the exact 0100 function body"
        )
        error, before, after = _probe(pg.async_url, null_only)
        assert error is not None and _REJECT_UPDATE in error, (
            "after downgrade even the raw_payload-only UPDATE is rejected again"
        )
        assert after == before
        error, _, _ = _probe(pg.async_url, "DELETE FROM audit_log WHERE id = :id")
        assert error is not None and _REJECT_DELETE in error
    finally:
        _alembic(pg.async_url, "upgrade", "head")

    assert asyncio.run(_function_src(pg.async_url)) != pg.strict_function_src
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
