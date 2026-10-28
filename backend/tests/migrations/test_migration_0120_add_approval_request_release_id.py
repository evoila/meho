# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0120_add_approval_request_release_id`` (#5030).

The migration adds one nullable column, ``approval_request.release_id``:
the MEHO release a request was made under. Rows saved before it keep
NULL, which counts as an older release.

Every step targets this migration's own revision (``0120``) and its
``down_revision`` (``0118``), never ``head``, so a later migration cannot
break these tests.
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

_REVISION = "0120"
_DOWN_REVISION = "0118"
_COLUMN = "release_id"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0120.db"
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


def _columns(sync_url: str) -> dict[str, bool]:
    """Return ``{column_name: is_nullable}`` for ``approval_request``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(approval_request)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]): int(row[3]) == 0 for row in rows}


def test_upgrade_adds_only_one_nullable_column(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    before = _columns(sync_url)

    command.upgrade(cfg, _REVISION)
    after = _columns(sync_url)

    assert set(after) - set(before) == {_COLUMN}
    assert after[_COLUMN], "release_id must be nullable"
    assert {k: v for k, v in after.items() if k != _COLUMN} == before


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _COLUMN in _columns(sync_url)
