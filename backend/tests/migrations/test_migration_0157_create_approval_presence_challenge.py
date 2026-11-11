# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0157_create_approval_presence_challenge``.

The migration creates ``approval_presence_challenge`` and its clean-up
index. ``downgrade()`` drops them. Every step targets this migration's own
revision (``0157``) and its ``down_revision`` (``0156``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0157"
_DOWN_REVISION = "0156"
_TABLE = "approval_presence_challenge"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0157.db"
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


def _inspect(sync_url: str) -> tuple[set[str], set[str], set[str]]:
    engine = sa_create_engine(sync_url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        if _TABLE not in tables:
            return tables, set(), set()
        columns = {c["name"] for c in insp.get_columns(_TABLE)}
        indexes = {i["name"] for i in insp.get_indexes(_TABLE)}
        return tables, columns, indexes
    finally:
        engine.dispose()


def test_up_creates_the_table_and_down_drops_it(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    assert _TABLE not in _inspect(sync_url)[0]

    command.upgrade(cfg, _REVISION)
    tables, columns, indexes = _inspect(sync_url)
    assert _TABLE in tables
    assert columns == {
        "id",
        "tenant_id",
        "approver_sub",
        "request_ids",
        "binding_hash",
        "server_random",
        "card_hash",
        "match_code",
        "created_at",
        "expires_at",
        "used_at",
        "channel",
        "session_kind",
    }
    assert "approval_presence_challenge_created_at_idx" in indexes

    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _inspect(sync_url)[0]

    command.upgrade(cfg, _REVISION)
    assert _TABLE in _inspect(sync_url)[0]
