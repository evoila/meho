# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0070_add_sensor_confirmation_retries``.

Initiative #2780, Task #2799. Adds the per-sensor state-confirmation
config + soft-state window the ``record_sensor_result`` commit gate reads:

* ``retry_times`` -- ``integer`` NOT NULL, server default ``'0'``. The
  ``'0'`` backfill disables confirmation for every pre-#2799 row, so the
  migration is behaviour-preserving on its own.
* ``retry_backoff_seconds`` -- ``integer`` NOT NULL, server default ``'15'``.
* ``pending_state`` -- ``text`` nullable, CHECK over ``CheckState`` minus
  ``skip``; NULL (the backfilled state) means no window is open.
* ``pending_count`` -- ``integer`` NOT NULL, server default ``'0'``.

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0070``) and its
``down_revision`` (``0069``), never ``head`` -- so a future head migration
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

_REVISION = "0070"
_DOWN_REVISION = "0069"
_CONFIRMATION_COLUMNS = (
    "retry_times",
    "retry_backoff_seconds",
    "pending_state",
    "pending_count",
)

#: A pre-0070 ``sensor`` row. SQLite does not enforce foreign keys by
#: default, so no parent tenant row is needed. UUIDs are stored as 32-char
#: hex (SQLAlchemy's ``Uuid`` on SQLite drops the dashes).
_SENSOR_ID = "77777777777777777777777777777777"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0070.db"
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
    """Return ``(column_name, is_nullable)`` pairs for ``sensor``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(sensor)")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 0) for row in rows]


def _columns(sync_url: str) -> set[str]:
    return {name for name, _ in _table_info(sync_url)}


def _column_is_nullable(sync_url: str, column: str) -> bool:
    for name, nullable in _table_info(sync_url):
        if name == column:
            return nullable
    raise AssertionError(f"column {column!r} not present on sensor")


def _insert_pre_0070_sensor(sync_url: str) -> None:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sensor "
                    "(id, tenant_id, name, connector_id, op_id, params, "
                    "assertion, cadence_kind, interval_seconds, "
                    "created_by_sub, created_at, updated_at) "
                    "VALUES (:id, '11111111111111111111111111111111', "
                    "'disk-space', 'vmware-rest-9.0', 'vmware.vm.list', "
                    "'{}', '{}', 'interval', 60, 'seed', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                ),
                {"id": _SENSOR_ID},
            )
    finally:
        sync_eng.dispose()


def test_upgrade_adds_confirmation_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0070`` lands all four columns with the right nullability."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _columns(sync_url)
    for column in _CONFIRMATION_COLUMNS:
        assert column in columns, f"migration 0070 must add sensor.{column}"

    assert _column_is_nullable(sync_url, "pending_state")
    # The config knobs + counter are NOT NULL (server defaults) so the
    # commit gate always reads concrete values.
    assert not _column_is_nullable(sync_url, "retry_times")
    assert not _column_is_nullable(sync_url, "retry_backoff_seconds")
    assert not _column_is_nullable(sync_url, "pending_count")


def test_existing_row_backfills_confirmation_off(alembic_cfg: tuple[Config, str]) -> None:
    """A row inserted at ``0069`` backfills retry_times=0 (confirmation off).

    The behaviour-preserving property: no pre-#2799 sensor starts holding
    soft states when the migration lands, because ``retry_times=0`` is the
    off switch.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    _insert_pre_0070_sensor(sync_url)

    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT retry_times, retry_backoff_seconds, pending_state, "
                    "pending_count FROM sensor WHERE id = :id"
                ),
                {"id": _SENSOR_ID},
            ).one()
    finally:
        sync_eng.dispose()
    assert row[0] == 0, "pre-0070 rows must backfill with confirmation off"
    assert row[1] == 15, "pre-0070 rows must backfill the default backoff"
    assert row[2] is None, "no confirmation window may be open after backfill"
    assert row[3] == 0


def test_pending_state_check_rejects_skip(alembic_cfg: tuple[Config, str]) -> None:
    """The CHECK admits ``CheckState`` minus ``skip`` -- not ``skip`` itself."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with pytest.raises(IntegrityError), sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sensor "
                    "(id, tenant_id, name, connector_id, op_id, params, "
                    "assertion, cadence_kind, interval_seconds, pending_state, "
                    "created_by_sub, created_at, updated_at) "
                    "VALUES ('88888888888888888888888888888888', "
                    "'11111111111111111111111111111111', 'bad', "
                    "'vmware-rest-9.0', 'vmware.vm.list', '{}', '{}', "
                    "'interval', 60, 'skip', 'seed', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                )
            )
    finally:
        sync_eng.dispose()


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0069"`` drops the columns; ``upgrade "0070"`` restores them.

    Pinned to this migration's own revision on both legs (never ``head``) so a
    future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert set(_CONFIRMATION_COLUMNS) <= _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    remaining = _columns(sync_url)
    for column in _CONFIRMATION_COLUMNS:
        assert column not in remaining, f"downgrade must drop {column}"

    command.upgrade(cfg, _REVISION)
    assert set(_CONFIRMATION_COLUMNS) <= _columns(sync_url)
