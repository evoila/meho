# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0053_add_approval_request_session_lineage``.

Initiative #2151, Task #2086 (approval-lineage replay gap). Adds the two
nullable soft-FK lineage columns to ``approval_request``:

* ``agent_session_id`` -- the session the parking dispatch belonged to.
* ``request_audit_id`` -- the ``approval.request`` audit row's id, the
  parent every later lifecycle audit row back-links to.

Soft-column discipline mirrors ``0036`` / ``0040``: nullable, no server
default, reversible, no indexes. The round-trip test pins its downgrade
target to this migration's **own** ``down_revision`` (``0052``) so a
future head migration cannot break it (the stamp-replay idempotency
footgun that recurred on 0049/0050).

Mirrors :mod:`tests.test_migration_0040_approval_request_work_ref`;
SQLite is the test driver and the migration uses only generic DDL, so PG
parity holds.
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

_LINEAGE_COLUMNS = ("agent_session_id", "request_audit_id")


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0053.db"
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


def _approval_request_table_info(sync_url: str) -> list[tuple[str, bool]]:
    """Return ``(column_name, is_nullable)`` pairs for ``approval_request``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(approval_request)")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 0) for row in rows]


def _approval_request_columns(sync_url: str) -> set[str]:
    return {name for name, _ in _approval_request_table_info(sync_url)}


def _column_is_nullable(sync_url: str, column: str) -> bool:
    for name, nullable in _approval_request_table_info(sync_url):
        if name == column:
            return nullable
    raise AssertionError(f"column {column!r} not present on approval_request")


def test_upgrade_adds_both_lineage_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands both nullable lineage columns."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _approval_request_columns(sync_url)
    for column in _LINEAGE_COLUMNS:
        assert column in columns, f"migration 0053 must add approval_request.{column}"
        assert _column_is_nullable(sync_url, column), (
            f"{column} must be nullable -- NULL means 'lineage unknown' "
            "(pre-0053 rows, or a park outside any session)"
        )


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0052"`` drops both columns; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert set(_LINEAGE_COLUMNS) <= _approval_request_columns(sync_url)

    command.downgrade(cfg, "0052")
    columns_after_downgrade = _approval_request_columns(sync_url)
    for column in _LINEAGE_COLUMNS:
        assert column not in columns_after_downgrade, f"downgrade must drop {column}"

    command.upgrade(cfg, "head")
    assert set(_LINEAGE_COLUMNS) <= _approval_request_columns(sync_url)
