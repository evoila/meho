# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0115_add_sandbox_mode`` (#4622).

* **An existing database** (upgraded from ``0114``, with no rows at all)
  gets the stored mode ``false``; a start with the mode on is refused.
* **A new database** (the whole chain in one upgrade) gets no stored
  mode; the first start stores it.
* ``sandbox_delete_grant`` exists with the same four indexes as
  ``service_principal_grant``; ``audit_log.sandbox_mode`` is not null,
  defaults to ``false``, and a row inserted without the column (as an
  older build writes it) reads ``false``.
* **Downgrade** drops the two tables and the column.

Every step targets this migration's own revision (``0115``) and its
``down_revision`` (``0114``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.sandbox_mode import SandboxModeConfigError, store_or_check_sandbox_mode
from meho_backplane.settings import get_settings

_REVISION = "0115"
_DOWN_REVISION = "0114"

#: An audit row as an older build writes it: no ``sandbox_mode`` column.
_OLD_BUILD_AUDIT_INSERT = (
    "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, payload) "
    "VALUES ('{id}', CURRENT_TIMESTAMP, 'old', 'GET', '{path}', 200, '{{}}')"
)
_SANDBOX_HOST = "meho.sandbox.test"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0115.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", f"https://{_SANDBOX_HOST}")
    monkeypatch.delenv("MEHO_SANDBOX_MODE", raising=False)
    monkeypatch.delenv("MEHO_SANDBOX_MODE_CONFIRM", raising=False)
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _fetch(sync_url: str, sql: str) -> list[Any]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return list(conn.execute(text(sql)).all())
    finally:
        eng.dispose()


def _execute(sync_url: str, sql: str) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(sql))
    finally:
        eng.dispose()


def _tables(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _indexes(sync_url: str, table: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        return {str(index["name"]) for index in inspect(eng).get_indexes(table)}
    finally:
        eng.dispose()


def _columns(sync_url: str, table: str) -> dict[str, dict[str, Any]]:
    eng = sa_create_engine(sync_url)
    try:
        return {column["name"]: column for column in inspect(eng).get_columns(table)}
    finally:
        eng.dispose()


def _stored_mode(sync_url: str) -> list[Any]:
    return _fetch(sync_url, "SELECT id, sandbox_mode FROM sandbox_mode_state")


def test_existing_database_stores_off_and_refuses_the_mode_on(
    alembic_cfg: tuple[Config, str],
    monkeypatch: pytest.MonkeyPatch,
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """A database at the previous revision, with no rows at all, is not a new install."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    assert _stored_mode(sync_url) == [(1, False)]

    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", _SANDBOX_HOST)
    get_settings.cache_clear()
    with pytest.raises(SandboxModeConfigError, match="installed with sandbox mode off"):
        asyncio.run(store_or_check_sandbox_mode(get_settings()))


def test_new_database_stores_nothing_until_the_first_start(
    alembic_cfg: tuple[Config, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain in one upgrade: no stored mode; the first start stores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _stored_mode(sync_url) == []

    monkeypatch.setenv("MEHO_SANDBOX_MODE", "true")
    monkeypatch.setenv("MEHO_SANDBOX_MODE_CONFIRM", _SANDBOX_HOST)
    get_settings.cache_clear()
    assert asyncio.run(store_or_check_sandbox_mode(get_settings())) is True
    assert _stored_mode(sync_url) == [(1, True)]


def test_state_table_holds_one_row(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _execute(
            sync_url,
            "INSERT INTO sandbox_mode_state (id, sandbox_mode, stored_at) "
            "VALUES (2, true, CURRENT_TIMESTAMP)",
        )


def test_delete_grant_table_mirrors_the_ordinary_grant_table(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    tables = _tables(sync_url)
    assert {"sandbox_delete_grant", "sandbox_mode_state"} <= tables
    ordinary = _columns(sync_url, "service_principal_grant")
    sandbox = _columns(sync_url, "sandbox_delete_grant")
    assert set(ordinary) == set(sandbox)
    assert _indexes(sync_url, "sandbox_delete_grant") == {
        "sandbox_delete_grant_lookup_idx",
        "uq_sandbox_delete_grant_targeted",
        "uq_sandbox_delete_grant_targetless",
        "sandbox_delete_grant_expires_at_idx",
    }


def test_audit_mark_defaults_to_false(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """Existing rows read false, and so does a row written without the column."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _execute(sync_url, _OLD_BUILD_AUDIT_INSERT.format(id="1" * 32, path="/x"))
    command.upgrade(cfg, _REVISION)
    column = _columns(sync_url, "audit_log")["sandbox_mode"]
    assert column["nullable"] is False
    # A row an older build writes: no sandbox_mode in the column list.
    _execute(sync_url, _OLD_BUILD_AUDIT_INSERT.format(id="2" * 32, path="/y"))
    rows = _fetch(sync_url, "SELECT operator_sub, sandbox_mode FROM audit_log ORDER BY path")
    assert [bool(mode) for _sub, mode in rows] == [False, False]


def test_downgrade_drops_the_tables_and_the_column(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    tables = _tables(sync_url)
    assert "sandbox_delete_grant" not in tables
    assert "sandbox_mode_state" not in tables
    assert "sandbox_mode" not in _columns(sync_url, "audit_log")
    command.upgrade(cfg, _REVISION)
    assert {"sandbox_delete_grant", "sandbox_mode_state"} <= _tables(sync_url)
