# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0068_add_check_dashboard_notify``.

Initiative #2716, Task #2719. Adds the per-Dashboard notification config the
checks notifier reads on every claimed rollup transition:

* ``notify_email`` -- ``text`` nullable; NULL means notifications are off,
  so every pre-#2719 row backfills to "silent" and the migration is
  behaviour-preserving on its own.
* ``notify_min_state`` -- ``text`` NOT NULL, server default ``'critical'``,
  CHECK ``IN ('degraded', 'critical')``.

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0068``) and its
``down_revision`` (``0067``), never ``head`` -- so a future head migration
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
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0068"
_DOWN_REVISION = "0067"
_NOTIFY_COLUMNS = ("notify_email", "notify_min_state")

#: A pre-0068 ``check_dashboards`` row. SQLite does not enforce foreign keys
#: by default, so no parent tenant row is needed. UUIDs are stored as 32-char
#: hex (SQLAlchemy's ``Uuid`` on SQLite drops the dashes).
_DASHBOARD_ID = "55555555555555555555555555555555"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0068.db"
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


def _column_is_nullable(sync_url: str, column: str) -> bool:
    for name, nullable in _table_info(sync_url):
        if name == column:
            return nullable
    raise AssertionError(f"column {column!r} not present on check_dashboards")


def _insert_pre_0068_dashboard(sync_url: str) -> None:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO check_dashboards "
                    "(id, tenant_id, name, description, last_rollup_state, "
                    "created_by_sub, created_at, updated_at) "
                    "VALUES (:id, '11111111111111111111111111111111', 'prod', NULL, "
                    "'critical', 'seed', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                ),
                {"id": _DASHBOARD_ID},
            )
    finally:
        sync_eng.dispose()


def test_upgrade_adds_notify_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0068`` lands both columns with the right nullability."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _columns(sync_url)
    for column in _NOTIFY_COLUMNS:
        assert column in columns, f"migration 0068 must add check_dashboards.{column}"

    assert _column_is_nullable(sync_url, "notify_email")
    # notify_min_state is NOT NULL (server default 'critical') so the
    # notifier's rank comparison always reads a concrete state.
    assert not _column_is_nullable(sync_url, "notify_min_state")


def test_existing_row_backfills_silent_and_critical(alembic_cfg: tuple[Config, str]) -> None:
    """A row inserted at ``0067`` backfills NULL email + ``'critical'`` floor.

    The behaviour-preserving property: no pre-#2719 Dashboard starts emailing
    when the migration lands, because the recipient column is the off switch.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    _insert_pre_0068_dashboard(sync_url)

    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT notify_email, notify_min_state FROM check_dashboards WHERE id = :id"),
                {"id": _DASHBOARD_ID},
            ).one()
    finally:
        sync_eng.dispose()
    assert row[0] is None, "pre-0068 rows must backfill with notifications off"
    assert row[1] == "critical", "pre-0068 rows must backfill the conservative floor"


def test_notify_min_state_check_rejects_out_of_vocabulary(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The CHECK admits only ``degraded`` / ``critical`` -- not ``ok``."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with pytest.raises(IntegrityError), sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO check_dashboards "
                    "(id, tenant_id, name, notify_min_state, created_by_sub, "
                    "created_at, updated_at) "
                    "VALUES ('66666666666666666666666666666666', "
                    "'11111111111111111111111111111111', 'bad', 'ok', 'seed', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                )
            )
    finally:
        sync_eng.dispose()


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0067"`` drops the columns; ``upgrade "0068"`` restores them.

    Pinned to this migration's own revision on both legs (never ``head``) so a
    future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert set(_NOTIFY_COLUMNS) <= _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    remaining = _columns(sync_url)
    for column in _NOTIFY_COLUMNS:
        assert column not in remaining, f"downgrade must drop {column}"

    command.upgrade(cfg, _REVISION)
    assert set(_NOTIFY_COLUMNS) <= _columns(sync_url)
