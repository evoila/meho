# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0182_add_audit_log_secret_read_as``.

* The upgrade adds one nullable text column on ``audit_log``:
  ``secret_read_as``. Other columns stay as they are.
* The downgrade drops the column again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0182"
_DOWN_REVISION = "0181"
_COLUMN = "secret_read_as"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0182.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _columns(sync_url: str) -> dict[str, dict[str, Any]]:
    eng = sa_create_engine(sync_url)
    try:
        return {col["name"]: dict(col) for col in inspect(eng).get_columns("audit_log")}
    finally:
        eng.dispose()


def test_upgrade_adds_the_nullable_column(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    before = _columns(sync_url)
    assert _COLUMN not in before

    command.upgrade(cfg, _REVISION)
    after = _columns(sync_url)

    assert set(after) - set(before) == {_COLUMN}
    assert after[_COLUMN]["nullable"] is True


def test_downgrade_drops_the_column(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url)


def test_upgrade_has_no_default_and_runs_no_update() -> None:
    """The column has no default and the file runs no SQL statement of its own."""
    source = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0182_add_audit_log_secret_read_as.py"
    ).read_text(encoding="utf-8")
    assert "server_default" not in source
    assert "op.execute" not in source
