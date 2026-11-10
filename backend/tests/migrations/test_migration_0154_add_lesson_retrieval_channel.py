# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0154_add_lesson_retrieval_channel``.

The migration adds ``lesson_retrieval.channel``. A row written before the
upgrade reads ``search``, and ``downgrade()`` drops the column again, on an
empty and on a filled table.

Every step targets this migration's own revision (``0154``) and its
``down_revision`` (``0153``), never ``head``.
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
from meho_backplane.settings import get_settings

_REVISION = "0154"
_DOWN_REVISION = "0153"
_ROW_ID = "a" * 32
_TENANT_ID = "b" * 32
_ENTRY_ID = "c" * 32
_SESSION_ID = "d" * 32


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0154.db"
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


def _columns(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return {column["name"] for column in inspect(engine).get_columns("lesson_retrieval")}
    finally:
        engine.dispose()


def _insert_row(sync_url: str) -> None:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lesson_retrieval (id, tenant_id, store, entry_id, "
                    "entry_tenant_id, agent_session_id, retrieved_at, status, expired) "
                    "VALUES (:id, :tenant, 'kb', :entry, :tenant, :session, "
                    "'2026-11-08 00:00:00', 'confirmed', 0)"
                ),
                {"id": _ROW_ID, "tenant": _TENANT_ID, "entry": _ENTRY_ID, "session": _SESSION_ID},
            )
    finally:
        engine.dispose()


def _channels(sync_url: str) -> list[str]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            return list(conn.execute(text("SELECT channel FROM lesson_retrieval")).scalars())
    finally:
        engine.dispose()


def test_an_old_row_reads_search(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_row(sync_url)

    command.upgrade(cfg, _REVISION)

    assert _channels(sync_url) == ["search"]


def test_up_and_down_on_an_empty_table(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)

    command.upgrade(cfg, _REVISION)
    assert "channel" in _columns(sync_url)
    command.downgrade(cfg, _DOWN_REVISION)
    assert "channel" not in _columns(sync_url)


def test_down_on_a_filled_table_keeps_the_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_row(sync_url)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _DOWN_REVISION)

    assert "channel" not in _columns(sync_url)
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            ids = list(conn.execute(text("SELECT id FROM lesson_retrieval")).scalars())
    finally:
        engine.dispose()
    assert ids == [_ROW_ID]
