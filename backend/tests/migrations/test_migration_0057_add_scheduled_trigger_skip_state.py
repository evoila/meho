# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0057_add_scheduled_trigger_skip_state``.

Initiative #2364, Task #2327. Adds the skip-state projection columns to
``scheduled_trigger`` so the tick loop's silent every-tick precondition
skips become visible on the row:

* ``last_skip_reason`` -- ``text`` nullable; machine tag of the most recent
  skip cause.
* ``last_skipped_at`` -- ``timestamptz`` nullable; UTC time of the most
  recent skip.
* ``skip_count`` -- ``integer`` NOT NULL, server default ``0``; consecutive
  skips since the last successful fire.

Soft-column discipline mirrors ``0043`` / ``0053`` / ``0055``: additive,
reversible, the two nullable columns take no server default, ``skip_count``
carries a ``0`` server default so pre-#2327 rows backfill to "never
skipped".

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0057``) and
its ``down_revision`` (``0056``), never ``head`` -- so a future head
migration that adds another column cannot make ``upgrade("head")`` re-run
this ``add_column`` on a row that already has it (the duplicate-column
stamp-replay footgun). SQLite is the test driver and the migration uses
only generic DDL, so PG parity holds.
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

_REVISION = "0057"
_DOWN_REVISION = "0056"
_SKIP_COLUMNS = ("last_skip_reason", "last_skipped_at", "skip_count")


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0057.db"
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


def _scheduled_trigger_table_info(sync_url: str) -> list[tuple[str, bool]]:
    """Return ``(column_name, is_nullable)`` pairs for ``scheduled_trigger``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(scheduled_trigger)")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 0) for row in rows]


def _scheduled_trigger_columns(sync_url: str) -> set[str]:
    return {name for name, _ in _scheduled_trigger_table_info(sync_url)}


def _column_is_nullable(sync_url: str, column: str) -> bool:
    for name, nullable in _scheduled_trigger_table_info(sync_url):
        if name == column:
            return nullable
    raise AssertionError(f"column {column!r} not present on scheduled_trigger")


def test_upgrade_adds_skip_state_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0057`` lands all three skip-state columns with the right nullability."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _scheduled_trigger_columns(sync_url)
    for column in _SKIP_COLUMNS:
        assert column in columns, f"migration 0057 must add scheduled_trigger.{column}"

    assert _column_is_nullable(sync_url, "last_skip_reason")
    assert _column_is_nullable(sync_url, "last_skipped_at")
    # skip_count is NOT NULL (server default 0) so every row always carries
    # a concrete count -- the loop's park arithmetic reads an int, never None.
    assert not _column_is_nullable(sync_url, "skip_count")


def test_skip_count_backfills_zero_on_existing_row(alembic_cfg: tuple[Config, str]) -> None:
    """A row inserted at ``0056`` backfills ``skip_count=0`` when 0057 lands.

    Proves the server default covers pre-#2327 rows without a data-migration
    pass -- the "never skipped" starting state.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    # Insert a minimal cron trigger directly at the pre-0057 schema. SQLite
    # does not enforce foreign keys by default, so the row needs no parent
    # tenant / agent_definition -- the discriminated-union CHECK
    # (kind='cron' => cron_expr NOT NULL, fire_at/event_filter NULL) and the
    # NOT-NULL columns are all this test cares about. UUIDs are stored as
    # 32-char hex (SQLAlchemy's ``Uuid`` on SQLite drops the dashes).
    trigger_id = "33333333333333333333333333333333"
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO scheduled_trigger "
                    "(id, tenant_id, agent_definition_id, kind, cron_expr, timezone, "
                    "status, in_flight_policy, next_fire_at, identity_sub, created_by_sub, "
                    "created_at, updated_at) "
                    "VALUES (:id, '11111111111111111111111111111111', "
                    "'22222222222222222222222222222222', 'cron', '*/5 * * * *', 'UTC', "
                    "'active', 'fail_into_audit', '2026-01-01T00:05:00+00:00', "
                    "'__scheduler__', 'seed', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                ),
                {"id": trigger_id},
            )
    finally:
        sync_eng.dispose()

    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT skip_count, last_skip_reason, last_skipped_at "
                    "FROM scheduled_trigger WHERE id = :id"
                ),
                {"id": trigger_id},
            ).one()
    finally:
        sync_eng.dispose()
    assert row[0] == 0, "pre-0057 row must backfill skip_count=0 via the server default"
    assert row[1] is None
    assert row[2] is None


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0056"`` drops the columns; ``upgrade "0057"`` restores them.

    Pinned to this migration's own revision on both legs (never ``head``)
    so a future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert set(_SKIP_COLUMNS) <= _scheduled_trigger_columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    remaining = _scheduled_trigger_columns(sync_url)
    for column in _SKIP_COLUMNS:
        assert column not in remaining, f"downgrade must drop {column}"

    command.upgrade(cfg, _REVISION)
    assert set(_SKIP_COLUMNS) <= _scheduled_trigger_columns(sync_url)
