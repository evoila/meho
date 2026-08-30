# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0086_add_approval_request_preview_hash``.

#3197 / #3183 (governed deletes). Adds the nullable
``approval_request.preview_hash`` column — the preview-result-hash binding
recorded at park time for a ``destructive``-tier op and re-verified at
approve time, distinct from the existing ``params_hash``.

The migration is a plain nullable ``ADD COLUMN``, so the behaviour under
test is schema shape: the column is present + nullable after ``upgrade``,
absent after ``downgrade``, and the pair round-trips idempotently. SQLite
is the test driver; the migration uses only generic ``ADD COLUMN`` /
``DROP COLUMN`` DDL, so PostgreSQL parity holds.

The ``down_revision`` is ``0085`` (``0085_create_dispatch_trace_store``,
the head on ``origin/main``): the chain is the single linear head
``0084 -> 0085 -> 0086``. This test targets the literal revision ids.
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

_REVISION = "0086"
_DOWN_REVISION = "0085"
_TABLE = "approval_request"
_COLUMN = "preview_hash"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0086.db"
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


def _preview_hash_column(sync_url: str) -> dict[str, object] | None:
    """Return the ``approval_request.preview_hash`` column spec, or ``None``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            columns = inspect(conn).get_columns(_TABLE)
    finally:
        sync_eng.dispose()
    for column in columns:
        if column["name"] == _COLUMN:
            return column
    return None


def test_upgrade_adds_nullable_preview_hash(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0086`` adds ``preview_hash`` as a nullable column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    column = _preview_hash_column(sync_url)
    assert column is not None, "preview_hash column must exist after upgrade"
    # Nullable: a non-destructive / pre-0086 row legitimately carries no binding.
    assert column["nullable"] is True


def test_downgrade_drops_preview_hash(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0084`` drops the ``preview_hash`` column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _preview_hash_column(sync_url) is not None

    command.downgrade(cfg, _DOWN_REVISION)
    assert _preview_hash_column(sync_url) is None


def test_upgrade_round_trips_after_clean_downgrade(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0086`` → ``downgrade 0084`` → ``upgrade 0086`` restores the column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert _preview_hash_column(sync_url) is not None
