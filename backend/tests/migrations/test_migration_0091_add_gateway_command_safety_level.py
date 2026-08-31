# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0091`` (#3192).

Initiative #2901 (satellite write path), Task #3192 — revocation hardening.
Adds the ``safety_level`` column to ``gateway_command`` so the delivery path
can tier-scope the write-capable-runner revocation refusal.

Asserts the column lands after ``upgrade 0091``, round-trips (downgrade to
``0087`` drops it, re-upgrade re-adds it), and that the NOT NULL ADD COLUMN
lands on the empty clean-slate table via its ``'safe'`` server default.

**Idempotency pinning (0049/0050/0055 footgun).** Every forward / round-trip
step targets this migration's **own** revision (``0091``) and its
``down_revision`` (``0087``), never ``head`` — so a future head migration
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

_REVISION = "0091"
_DOWN_REVISION = "0087"
_TABLE = "gateway_command"
_COLUMN = "safety_level"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0091.db"
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


def test_upgrade_adds_safety_level_column(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0091`` adds the ``safety_level`` column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _COLUMN in _columns(sync_url)


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0087"`` drops the column; ``upgrade "0091"`` re-adds it.

    Pinned to this migration's own revision on both legs (never ``head``) so
    a future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)


def test_safety_level_is_not_null_with_safe_default(alembic_cfg: tuple[Config, str]) -> None:
    """The NOT NULL ``safety_level`` ADD COLUMN lands on the empty table.

    A raw insert that omits ``safety_level`` succeeds via its constant
    ``'safe'`` server default (the read-tier fail-safe) — proving the ADD
    COLUMN is valid on the empty clean-slate table across dialects rather
    than requiring a backfill, and that a row that never named a level is a
    read (never a remote-write the revocation filter would refuse).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            # FK enforcement stays off (SQLite default) so the bogus tenant_id
            # does not mask the column under test.
            conn.execute(
                text(
                    "INSERT INTO gateway_command "
                    "(id, tenant_id, runner_id, op_id, params, status, "
                    "enqueued_by_sub, enqueued_at, params_hash, expires_at) "
                    "VALUES ('cmd-1', 'ten-1', 'runner-a', 'net.ping', '{}', "
                    "'pending', 'sub-1', '2026-08-31', 'h', '2026-08-31')"
                )
            )
            safety_level = conn.execute(
                text("SELECT safety_level FROM gateway_command WHERE id = 'cmd-1'")
            ).scalar_one()
    finally:
        sync_eng.dispose()

    assert safety_level == "safe"
