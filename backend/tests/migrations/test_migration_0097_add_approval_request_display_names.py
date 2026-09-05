# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0097_add_approval_request_display_names``.

#3300 / #3301 (console + CLI GUID-to-name resolution). Adds the two
nullable ``approval_request`` display-name columns -- ``principal_name``
(requester name captured at park time) and ``reviewed_by_name`` (reviewer
name captured at decision time) -- so approvals surfaces render a human
name alongside the ``sub`` GUID, fail-open to the GUID when no name was
recorded.

The migration is a pair of plain nullable ``ADD COLUMN`` statements, so the
behaviour under test is schema shape: both columns are present + nullable
after ``upgrade``, absent after ``downgrade``, and the pair round-trips
idempotently. SQLite is the test driver; the migration uses only generic
``ADD COLUMN`` / ``DROP COLUMN`` DDL, so PostgreSQL parity holds.

The ``down_revision`` is ``0096`` (``0096_add_approval_request_resume_result``,
the head on ``origin/main``): the chain is the single linear head
``0095 -> 0096 -> 0097``. This test targets the literal revision ids.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0097"
_DOWN_REVISION = "0096"
_TABLE = "approval_request"
_COLUMNS = ("principal_name", "reviewed_by_name")


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0097.db"
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


def _column_specs(sync_url: str) -> dict[str, dict[str, object]]:
    """Return the ``approval_request`` columns keyed by name."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            columns = inspect(conn).get_columns(_TABLE)
    finally:
        sync_eng.dispose()
    return {column["name"]: column for column in columns}


def test_upgrade_adds_nullable_display_name_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0097`` adds both display-name columns as nullable."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    specs = _column_specs(sync_url)
    for name in _COLUMNS:
        assert name in specs, f"{name} column must exist after upgrade"
        # Nullable: a token without a name claim / a pre-0097 row / an
        # undecided request legitimately carries no name (fail-open).
        assert specs[name]["nullable"] is True


def test_downgrade_drops_display_name_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0096`` drops both display-name columns."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert all(name in _column_specs(sync_url) for name in _COLUMNS)

    command.downgrade(cfg, _DOWN_REVISION)
    specs = _column_specs(sync_url)
    for name in _COLUMNS:
        assert name not in specs


def test_upgrade_round_trips_after_clean_downgrade(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0097`` → ``downgrade 0096`` → ``upgrade 0097`` restores both columns."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert all(name in _column_specs(sync_url) for name in _COLUMNS)
