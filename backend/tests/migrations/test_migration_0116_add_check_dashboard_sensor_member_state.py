# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0116_add_check_dashboard_sensor_member_state``.

Task #5068 (parent goal #221). Adds the per-``(dashboard, sensor)`` memo
the transition claim compares each member's counted state against, so a
member that gets worse on an already-red Dashboard still mails:

* ``last_member_state`` -- ``text`` nullable, CHECK over the five-state
  vocabulary plus NULL. NULL means "not counted yet": every pre-0116 link
  row backfills to it, and the first claim records the state without
  mailing, so the migration is behaviour-preserving on its own.

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0116``) and
its ``down_revision`` (``0115``), never ``head``. SQLite is the test
driver; the migration uses only generic DDL through ``batch_alter_table``,
so PG parity holds.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0116"
_DOWN_REVISION = "0115"
_COLUMN = "last_member_state"

#: A pre-0116 link row. SQLite does not enforce foreign keys by default, so
#: no parent dashboard / sensor rows are needed. UUIDs are stored as 32-char
#: hex (SQLAlchemy's ``Uuid`` on SQLite drops the dashes).
_DASHBOARD_ID = "55555555555555555555555555555555"
_SENSOR_ID = "66666666666666666666666666666666"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0116.db"
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
    """Return ``(column_name, is_nullable)`` pairs for ``check_dashboard_sensors``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(check_dashboard_sensors)")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 0) for row in rows]


def _columns(sync_url: str) -> set[str]:
    return {name for name, _ in _table_info(sync_url)}


def _insert_link(sync_url: str, *, extra_columns: str = "", extra_values: str = "") -> None:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO check_dashboard_sensors "
                    f"(dashboard_id, sensor_id{extra_columns}) "
                    f"VALUES (:dashboard_id, :sensor_id{extra_values})"
                ),
                {"dashboard_id": _DASHBOARD_ID, "sensor_id": _SENSOR_ID},
            )
    finally:
        sync_eng.dispose()


def test_upgrade_adds_nullable_member_state(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """``upgrade 0116`` lands the column, nullable."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    info = dict(_table_info(sync_url))
    assert _COLUMN in info, f"migration 0116 must add check_dashboard_sensors.{_COLUMN}"
    assert info[_COLUMN], "the memo is nullable -- NULL means not counted yet"


def test_existing_link_backfills_null(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """A link row inserted at ``0115`` reads back with a NULL memo.

    The behaviour-preserving property: the first claim after the upgrade
    records each member's state silently instead of mailing for problems
    that are already on the Dashboard.
    """
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_link(sync_url)

    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {_COLUMN} FROM check_dashboard_sensors "
                    "WHERE dashboard_id = :d AND sensor_id = :s"
                ),
                {"d": _DASHBOARD_ID, "s": _SENSOR_ID},
            ).one()
    finally:
        sync_eng.dispose()
    assert row[0] is None, "pre-0116 link rows must backfill to 'not counted yet'"


def test_member_state_check_rejects_out_of_vocabulary(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """The CHECK admits the five states (and NULL) -- not an arbitrary word."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    _insert_link(sync_url, extra_columns=f", {_COLUMN}", extra_values=", 'critical'")
    with pytest.raises(IntegrityError):
        _insert_link(sync_url, extra_columns=f", {_COLUMN}", extra_values=", 'bogus'")


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """``downgrade "0115"`` drops the column; ``upgrade "0116"`` restores it.

    Pinned to this migration's own revision on both legs (never ``head``) so a
    future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url), "downgrade must drop the column"

    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)
