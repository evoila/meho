# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0059_create_runner_assignment_tables``.

Initiative #2415, Task #2499. Creates the two gateway-owned tables —
``runner_assignments`` (one authored document per runner) and
``runner_check_results`` (ingested execution reports). The **third**
migration in the initiative's serialized chain; extends the then-current
single head ``0058`` (``runner_principal``).

**Idempotency pinning (0049/0050/0053/0055/0057/0058 footgun).** Every
forward / round-trip / stamp-replay step targets this migration's **own**
revision (``0059``) and its ``down_revision`` (``0058``), never ``head`` —
so a future head migration cannot make ``upgrade("head")`` re-run these
``create_table`` calls on a schema that already has them. SQLite drives the
test and the migration uses only generic DDL, so PG parity holds.
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

_REVISION = "0059"
_DOWN_REVISION = "0058"

_ASSIGNMENTS_TABLE = "runner_assignments"
_RESULTS_TABLE = "runner_check_results"

_ASSIGNMENTS_COLUMNS = {
    "id",
    "tenant_id",
    "runner_name",
    "items",
    "created_at",
    "updated_at",
}
_RESULTS_COLUMNS = {
    "id",
    "tenant_id",
    "runner_name",
    "result_uid",
    "check_ref",
    "op_id",
    "status",
    "result_payload",
    "error",
    "received_at",
}
_UNIQUE_INDEXES = {
    "runner_assignments_tenant_runner_idx",
    "runner_check_results_uid_idx",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0059.db"
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


def _columns(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _index_names(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _unique_index_names(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA index_list columns: (seq, name, unique, origin, partial).
    return {str(row[1]) for row in rows if int(row[2]) == 1}


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _table_sql(sync_url: str, table: str) -> str:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            row = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"),
                {"n": table},
            ).first()
    finally:
        sync_eng.dispose()
    assert row is not None, f"table {table!r} not present"
    return str(row[0])


def test_upgrade_creates_both_tables(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0059`` creates both gateway tables with the full column sets."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url, _ASSIGNMENTS_TABLE) == _ASSIGNMENTS_COLUMNS
    assert _columns(sync_url, _RESULTS_TABLE) == _RESULTS_COLUMNS


def test_upgrade_creates_indexes(alembic_cfg: tuple[Config, str]) -> None:
    """The unique + staleness indexes are created."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _unique_index_names(sync_url, _ASSIGNMENTS_TABLE) >= {
        "runner_assignments_tenant_runner_idx"
    }
    assert _unique_index_names(sync_url, _RESULTS_TABLE) >= {"runner_check_results_uid_idx"}
    # The staleness index is non-unique; assert it exists at all.
    assert "runner_check_results_staleness_idx" in _index_names(sync_url, _RESULTS_TABLE)


def test_results_status_check_constraint(alembic_cfg: tuple[Config, str]) -> None:
    """The ``status`` CHECK bounds to the tri-state ``ok``/``refused``/``error``."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sql = _table_sql(sync_url, _RESULTS_TABLE)
    # The tri-state (matching the wire ``RunnerResult.status`` vocabulary) is
    # bounded, not a bare ``ok``/``error`` — ``refused`` must be permitted.
    assert "refused" in sql
    for value in ("ok", "refused", "error"):
        assert value in sql


def test_stamp_down_revision_then_upgrade_is_idempotent(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Stamp ``0058`` then upgrade to ``0059`` — pinned to own revisions, never ``head``.

    Builds the schema through the parent revision, stamps the version table
    to ``0058``, then replays **only** this migration to its own revision.
    A future head migration cannot make this step re-run the ``create_table``
    on an already-migrated schema, since no leg targets ``"head"``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    command.stamp(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert _ASSIGNMENTS_TABLE in _table_names(sync_url)
    assert _RESULTS_TABLE in _table_names(sync_url)


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0058"`` drops both tables; ``upgrade "0059"`` recreates them.

    Pinned to this migration's own revision on both legs (never ``head``).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _ASSIGNMENTS_TABLE in _table_names(sync_url)
    assert _RESULTS_TABLE in _table_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    names = _table_names(sync_url)
    assert _ASSIGNMENTS_TABLE not in names
    assert _RESULTS_TABLE not in names

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, _ASSIGNMENTS_TABLE) == _ASSIGNMENTS_COLUMNS
    assert _columns(sync_url, _RESULTS_TABLE) == _RESULTS_COLUMNS
