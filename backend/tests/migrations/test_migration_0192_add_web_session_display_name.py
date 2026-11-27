# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0192_add_web_session_display_name``.

The migration adds a nullable ``display_name`` column to ``web_session``.
A session row from before the upgrade keeps working and has no
name; the downgrade drops the column again.

Every step targets this migration's own revision (``0192``) and its
``down_revision`` (``0191``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0192"
_DOWN_REVISION = "0191"
_SESSION_ID = "c" * 32
_TENANT_ID = "d" * 32
_TS = "2026-11-26 12:00:00.000000"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0192.db"
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


def _web_session_columns(sync_url: str) -> dict[str, bool]:
    """Return ``{column name: nullable}`` for ``web_session``."""
    engine = sa_create_engine(sync_url)
    try:
        return {c["name"]: c["nullable"] for c in sa_inspect(engine).get_columns("web_session")}
    finally:
        engine.dispose()


def _run(sync_url: str, sql: str) -> list[tuple[object, ...]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql))
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def test_upgrade_adds_a_nullable_column_and_old_rows_have_no_name(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    assert "display_name" not in _web_session_columns(sync_url)
    _run(
        sync_url,
        "INSERT INTO web_session (id, operator_sub, tenant_id, created_at, expires_at, "
        "access_token, refresh_token, last_seen_at) "
        f"VALUES ('{_SESSION_ID}', 'sub-before-upgrade', '{_TENANT_ID}', '{_TS}', '{_TS}', "
        f"x'00', x'00', '{_TS}')",
    )

    command.upgrade(cfg, _REVISION)

    columns = _web_session_columns(sync_url)
    assert columns["display_name"] is True
    assert _run(sync_url, "SELECT display_name FROM web_session") == [(None,)]


def test_downgrade_drops_the_column_and_keeps_the_row(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _run(
        sync_url,
        "INSERT INTO web_session (id, operator_sub, tenant_id, created_at, expires_at, "
        "access_token, refresh_token, last_seen_at, display_name) "
        f"VALUES ('{_SESSION_ID}', 'sub-with-name', '{_TENANT_ID}', '{_TS}', '{_TS}', "
        f"x'00', x'00', '{_TS}', x'01')",
    )

    command.downgrade(cfg, _DOWN_REVISION)

    assert "display_name" not in _web_session_columns(sync_url)
    assert _run(sync_url, "SELECT operator_sub FROM web_session") == [("sub-with-name",)]
