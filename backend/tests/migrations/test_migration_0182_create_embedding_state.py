# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0182_create_embedding_state``.

The migration adds the one-row table that records which embedding model made
the stored vectors. The tests check that ``upgrade`` creates it with the
columns of the model and writes one row with the model the install runs,
that ``downgrade`` removes it, and that the pair round-trips. SQLite is the
test driver.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import EmbeddingState
from meho_backplane.settings import get_settings

_REVISION = "0182"
_DOWN_REVISION = "0181"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0182.db"
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


def _columns(sync_url: str) -> set[str] | None:
    """Return the columns of ``embedding_state``, or ``None`` when it does not exist."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            if "embedding_state" not in inspector.get_table_names():
                return None
            return {column["name"] for column in inspector.get_columns("embedding_state")}
    finally:
        sync_eng.dispose()


def _rows(sync_url: str) -> list[tuple[int, str, int]]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            return [
                (row[0], row[1], row[2])
                for row in conn.execute(text("SELECT id, model, dimension FROM embedding_state"))
            ]
    finally:
        sync_eng.dispose()


def test_upgrade_creates_the_table_and_stores_the_default_model(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RETRIEVAL_EMBEDDING_MODEL", raising=False)
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    assert _columns(sync_url) == set(EmbeddingState.__table__.columns.keys())
    assert _rows(sync_url) == [(1, "BAAI/bge-small-en-v1.5", 384)]


def test_upgrade_stores_the_model_the_install_runs(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "example/made-up-model")
    command.upgrade(cfg, _REVISION)

    assert _rows(sync_url) == [(1, "example/made-up-model", 384)]


def test_downgrade_drops_the_table(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _columns(sync_url) is None


def test_upgrade_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    assert len(_rows(sync_url)) == 1
