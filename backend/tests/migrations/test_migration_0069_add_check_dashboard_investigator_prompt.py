# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0069_add_check_dashboard_investigator_prompt``.

Initiative #2716, Task #2721. Adds the operator-authored briefing context the
diagnose-only investigator appends after its server-built transition snapshot:

* ``investigator_prompt`` -- ``text`` nullable. NULL means the briefing is
  built exactly as it was pre-#2721, so every pre-existing row backfills to
  today's behaviour and the migration is behaviour-preserving on its own.

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0069``) and its
``down_revision`` (``0068``), never ``head`` -- so a future head migration
cannot make ``upgrade("head")`` re-run this ``add_column`` on a table that
already has it. SQLite is the test driver; the migration uses only generic
DDL through ``batch_alter_table``, so PG parity holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0069"
_DOWN_REVISION = "0068"
_COLUMN = "investigator_prompt"

#: A pre-0069 ``check_dashboards`` row. SQLite does not enforce foreign keys
#: by default, so no parent tenant row is needed. UUIDs are stored as 32-char
#: hex (SQLAlchemy's ``Uuid`` on SQLite drops the dashes).
_DASHBOARD_ID = "77777777777777777777777777777777"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0069.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _table_info(sync_url: str) -> list[tuple[str, bool]]:
    """Return ``(column_name, is_nullable)`` pairs for ``check_dashboards``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(check_dashboards)")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 0) for row in rows]


def _columns(sync_url: str) -> set[str]:
    return {name for name, _ in _table_info(sync_url)}


def _insert_pre_0069_dashboard(sync_url: str) -> None:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO check_dashboards "
                    "(id, tenant_id, name, description, last_rollup_state, "
                    "notify_email, notify_min_state, created_by_sub, created_at, updated_at) "
                    "VALUES (:id, '11111111111111111111111111111111', 'prod', NULL, "
                    "'critical', 'ops@example.test', 'critical', 'seed', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                ),
                {"id": _DASHBOARD_ID},
            )
    finally:
        sync_eng.dispose()


def test_upgrade_adds_nullable_prompt_column(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0069`` lands ``investigator_prompt`` as a nullable column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    info = dict(_table_info(sync_url))
    assert _COLUMN in info, "migration 0069 must add check_dashboards.investigator_prompt"
    assert info[_COLUMN], "investigator_prompt must be nullable (NULL = pre-#2721 briefing)"


def test_existing_row_backfills_null(alembic_cfg: tuple[Config, str]) -> None:
    """A row inserted at ``0068`` backfills a NULL prompt.

    The behaviour-preserving property: no pre-#2721 Dashboard's briefing
    changes when the migration lands, because NULL is the "unchanged" state.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    _insert_pre_0069_dashboard(sync_url)

    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT investigator_prompt FROM check_dashboards WHERE id = :id"),
                {"id": _DASHBOARD_ID},
            ).one()
    finally:
        sync_eng.dispose()
    assert row[0] is None, "pre-0069 rows must backfill with no operator prompt"


def test_notify_columns_survive_the_add(alembic_cfg: tuple[Config, str]) -> None:
    """0068's columns and CHECK survive 0069's batch block.

    ``batch_alter_table`` recreates the table on SQLite for operations the
    dialect cannot do in place; this asserts the adjacent notification config
    (and the row data in it) comes through the migration intact.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    _insert_pre_0069_dashboard(sync_url)

    command.upgrade(cfg, _REVISION)

    assert {"notify_email", "notify_min_state"} <= _columns(sync_url)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT notify_email, notify_min_state FROM check_dashboards WHERE id = :id"),
                {"id": _DASHBOARD_ID},
            ).one()
    finally:
        sync_eng.dispose()
    assert row[0] == "ops@example.test"
    assert row[1] == "critical"


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0068"`` drops the column; ``upgrade "0069"`` restores it.

    Pinned to this migration's own revision on both legs (never ``head``) so a
    future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url), "downgrade must drop investigator_prompt"

    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)
