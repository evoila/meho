# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0055_add_approval_request_resumed_at``.

Initiative #2286 (G0.30), Task #2293. Adds the nullable exactly-one-resumer
claim column to ``approval_request``:

* ``resumed_at`` -- the UTC time the winning resumer claimed the single
  post-approval execution, NULL while unclaimed. A one-way latch set by the
  conditional-UPDATE claim
  (:func:`~meho_backplane.operations.approval_queue.claim_resume`).

Soft-column discipline mirrors ``0040`` / ``0053``: nullable, no server
default, reversible, no indexes.

**Idempotency pinning (0049/0050/0053 footgun).** Every forward/round-trip
step targets this migration's **own** revision (``0055``) and its
``down_revision`` (``0054``), never ``head`` — so a future head migration
that adds another column cannot make ``upgrade("head")`` re-run this
``add_column`` on a row that already has it (the duplicate-column
stamp-replay footgun). SQLite is the test driver and the migration uses
only generic DDL, so PG parity holds.
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

_REVISION = "0055"
_DOWN_REVISION = "0054"
_CLAIM_COLUMN = "resumed_at"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0055.db"
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


def test_upgrade_adds_nullable_resumed_at(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0055`` lands the nullable ``resumed_at`` claim column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _approval_request_columns(sync_url)
    assert _CLAIM_COLUMN in columns, f"migration 0055 must add approval_request.{_CLAIM_COLUMN}"
    assert _column_is_nullable(sync_url, _CLAIM_COLUMN), (
        f"{_CLAIM_COLUMN} must be nullable -- NULL means 'never resumed' "
        "(pre-0055 rows, or a freshly-parked unclaimed request)"
    )


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0054"`` drops the column; ``upgrade "0055"`` restores it.

    Pinned to this migration's own revision on both legs (never ``head``)
    so a future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _CLAIM_COLUMN in _approval_request_columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _CLAIM_COLUMN not in _approval_request_columns(sync_url), (
        f"downgrade must drop {_CLAIM_COLUMN}"
    )

    command.upgrade(cfg, _REVISION)
    assert _CLAIM_COLUMN in _approval_request_columns(sync_url)
