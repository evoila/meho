# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0146_bm25_index_uses_custom_description`` (#4753).

The migration builds ``endpoint_descriptor_bm25_idx`` again, on the text the
keyword half of operation search reads. The index is PostgreSQL only, so on
SQLite both steps change nothing. These tests check that:

* the migration's frozen index expression is the one the search query uses
  (``BM25_DOCUMENT_SQL``), so PostgreSQL can use the index;
* its old expression is the one migration ``0005`` built;
* ``upgrade`` and ``downgrade`` run on SQLite and leave the indexes alone.

The PostgreSQL steps and the query plan are checked in
``tests/integration/test_bm25_custom_description_pg.py``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.operations._search import BM25_DOCUMENT_SQL
from meho_backplane.settings import get_settings

_REVISION = "0146"
_DOWN_REVISION = "0145"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_migration_{filename[:4]}", _VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _squash(sql: str) -> str:
    return " ".join(sql.split())


def test_new_expression_is_the_search_expression() -> None:
    migration = _load("0146_bm25_index_uses_custom_description.py")
    assert _squash(migration.NEW_DOCUMENT_SQL) == _squash(BM25_DOCUMENT_SQL)


def test_old_expression_is_the_one_of_migration_0005() -> None:
    migration = _load("0146_bm25_index_uses_custom_description.py")
    source = (_VERSIONS / "0005_create_endpoint_descriptor.py").read_text()
    # Migration 0005 writes its expression across adjacent string literals.
    joined = _squash(source.replace('"\n            "', ""))
    assert _squash(migration.OLD_DOCUMENT_SQL) in joined


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0146.db"
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


def _indexes(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {str(ix["name"]) for ix in inspect(conn).get_indexes("endpoint_descriptor")}
    finally:
        eng.dispose()


def test_upgrade_and_downgrade_change_nothing_on_sqlite(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    before = _indexes(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _indexes(sync_url) == before
    command.downgrade(cfg, _DOWN_REVISION)
    assert _indexes(sync_url) == before
