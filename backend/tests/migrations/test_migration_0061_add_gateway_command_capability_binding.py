# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0061`` (#2500).

Initiative #2415 (Remote execution gateway), Task #2500. Adds the four
single-use capability-binding columns to ``gateway_command``:
``params_hash`` / ``expires_at`` (NOT NULL, sentinel-defaulted) and
``consumed_at`` / ``mint_audit_id`` (nullable).

Asserts the columns land after ``upgrade 0061`` and that the migration
round-trips (downgrade to ``0060`` drops all four, re-upgrade re-adds them).

**Idempotency pinning (0049/0050/0055 footgun).** Every forward / round-trip
step targets this migration's **own** revision (``0061``) and its
``down_revision`` (``0060``), never ``head`` — so a future head migration
cannot make ``upgrade("head")`` re-run this ``add_column`` on a schema that
already has it. SQLite is the test driver and the migration uses only
generic DDL, so PG parity holds.
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

_REVISION = "0061"
_DOWN_REVISION = "0060"
_TABLE = "gateway_command"
_CAPABILITY_COLUMNS = {"params_hash", "expires_at", "consumed_at", "mint_audit_id"}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0061.db"
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


def _columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({_TABLE})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_capability_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0061`` adds the four capability-binding columns."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url) >= _CAPABILITY_COLUMNS


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0060"`` drops the four columns; ``upgrade "0061"`` re-adds them.

    Pinned to this migration's own revision on both legs (never ``head``) so
    a future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url) >= _CAPABILITY_COLUMNS

    command.downgrade(cfg, _DOWN_REVISION)
    assert not (_CAPABILITY_COLUMNS & _columns(sync_url)), (
        "downgrade must drop every capability-binding column"
    )

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url) >= _CAPABILITY_COLUMNS


def test_expires_at_is_not_null_with_sentinel_default(alembic_cfg: tuple[Config, str]) -> None:
    """The NOT NULL ``expires_at``/``params_hash`` ADD COLUMN lands on the empty table.

    A raw insert that omits the two NOT NULL capability columns succeeds via
    their constant server defaults (the migration-mechanics sentinels) —
    proving the ADD COLUMN is valid on the empty clean-slate table across
    dialects rather than requiring a backfill.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            # FK enforcement stays off (SQLite default) so the bogus tenant_id
            # does not mask the columns under test.
            conn.execute(
                text(
                    "INSERT INTO gateway_command "
                    "(id, tenant_id, runner_id, op_id, params, status, "
                    "enqueued_by_sub, enqueued_at) "
                    "VALUES ('cmd-1', 'ten-1', 'runner-a', 'net.ping', '{}', "
                    "'pending', 'sub-1', '2026-07-15')"
                )
            )
            row = conn.execute(
                text(
                    "SELECT params_hash, expires_at, consumed_at, mint_audit_id "
                    "FROM gateway_command WHERE id = 'cmd-1'"
                )
            ).one()
    finally:
        sync_eng.dispose()

    params_hash, expires_at, consumed_at, mint_audit_id = row
    assert params_hash == ""  # empty-string sentinel default
    assert expires_at is not None  # epoch sentinel default (NOT NULL satisfied)
    assert consumed_at is None  # nullable, no default
    assert mint_audit_id is None  # nullable, no default
