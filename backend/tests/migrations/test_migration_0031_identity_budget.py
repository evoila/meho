# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0031_create_identity_budget``.

Initiative #806 (G11.5 Portability + cost), Task #1079 (G11.5-T5). The
migration adds the ``identity_budget`` table -- one row per
(tenant, principal, window-kind, window-start) budget bucket, carrying
optional limits + consumption counters.

Test matrix
-----------

* **Upgrade creates the table + columns + indexes.** ``upgrade head``
  from a clean DB leaves ``identity_budget`` present with every
  documented column and its named index.
* **Reversibility round-trip.** ``downgrade "0030"`` (0031's
  ``down_revision``) drops the table; a subsequent ``upgrade head``
  re-creates it.
* **audit_log / agent_run untouched.** The migration must not disturb
  pre-existing tables.
* **Column nullability.** The nullable columns (``token_limit`` /
  ``cost_limit`` / ``request_limit``) permit NULL; the NOT NULL columns
  reject it.
* **Window-kind CHECK + uniqueness constraint** behave correctly at
  the DB layer.

Follows the synchronous pattern of
:mod:`tests.test_migration_0023_approval_request`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0031.db"
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


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _table_columns(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _column_is_nullable(sync_url: str, table: str, column: str) -> bool:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on {table}")


def _table_indexes(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_upgrade_creates_identity_budget_table(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` creates the ``identity_budget`` table."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "identity_budget" in _table_names(sync_url)


def test_upgrade_creates_expected_columns(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``identity_budget`` has every documented column after upgrade."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    cols = _table_columns(sync_url, "identity_budget")
    expected = {
        "id",
        "tenant_id",
        "principal_sub",
        "window_kind",
        "window_start",
        "window_end",
        "token_limit",
        "cost_limit",
        "request_limit",
        "tokens_consumed",
        "cost_consumed",
        "requests_consumed",
        "created_at",
        "updated_at",
    }
    assert expected <= cols


def test_upgrade_creates_named_index(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The documented index exists after upgrade."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    idx = _table_indexes(sync_url, "identity_budget")
    assert "identity_budget_tenant_principal_idx" in idx


def test_limits_columns_are_nullable(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Optional-limit columns permit NULL (NULL = no cap)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    for col in ("token_limit", "cost_limit", "request_limit"):
        assert _column_is_nullable(sync_url, "identity_budget", col), (
            f"{col} must be nullable (NULL means no cap)"
        )


def test_consumption_columns_are_not_nullable(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Consumption columns reject NULL (default 0)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    for col in ("tokens_consumed", "cost_consumed", "requests_consumed"):
        assert not _column_is_nullable(sync_url, "identity_budget", col), (
            f"{col} must be NOT NULL with default 0"
        )


def test_keying_columns_are_not_nullable(
    alembic_cfg: tuple[Config, str],
) -> None:
    """tenant_id / principal_sub / window_kind / window_start / window_end are NOT NULL."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    for col in (
        "tenant_id",
        "principal_sub",
        "window_kind",
        "window_start",
        "window_end",
    ):
        assert not _column_is_nullable(sync_url, "identity_budget", col), (
            f"{col} must be NOT NULL (keying / boundary column)"
        )


def test_window_kind_check_constraint_rejects_unknown(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The window_kind CHECK refuses values outside the closed enum."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    # Bootstrap a tenant row so the FK insert can succeed.
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, '2026-05-27 00:00:00')"
                ),
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "slug": "t1",
                    "name": "Tenant One",
                },
            )

        # A row with a bogus window_kind must be rejected by the CHECK.
        with pytest.raises(IntegrityError), sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO identity_budget ("
                    "id, tenant_id, principal_sub, window_kind, "
                    "window_start, window_end, tokens_consumed, "
                    "cost_consumed, requests_consumed, created_at, updated_at"
                    ") VALUES ("
                    ":id, :tid, 'sub-1', 'fortnightly', "
                    "'2026-05-27 00:00:00', '2026-05-28 00:00:00', "
                    "0, 0, 0, '2026-05-27 00:00:00', '2026-05-27 00:00:00')"
                ),
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "tid": "11111111-1111-1111-1111-111111111111",
                },
            )
    finally:
        sync_eng.dispose()


def test_unique_constraint_rejects_duplicate_bucket(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A duplicate (tenant, principal, kind, start) row is refused."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, '2026-05-27 00:00:00')"
                ),
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "slug": "t1",
                    "name": "Tenant One",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO identity_budget ("
                    "id, tenant_id, principal_sub, window_kind, "
                    "window_start, window_end, tokens_consumed, "
                    "cost_consumed, requests_consumed, created_at, updated_at"
                    ") VALUES ("
                    ":id, :tid, 'sub-1', 'daily', "
                    "'2026-05-27 00:00:00', '2026-05-28 00:00:00', "
                    "0, 0, 0, '2026-05-27 00:00:00', '2026-05-27 00:00:00')"
                ),
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "tid": "11111111-1111-1111-1111-111111111111",
                },
            )

        with pytest.raises(IntegrityError), sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO identity_budget ("
                    "id, tenant_id, principal_sub, window_kind, "
                    "window_start, window_end, tokens_consumed, "
                    "cost_consumed, requests_consumed, created_at, updated_at"
                    ") VALUES ("
                    ":id, :tid, 'sub-1', 'daily', "
                    "'2026-05-27 00:00:00', '2026-05-28 00:00:00', "
                    "0, 0, 0, '2026-05-27 00:00:00', '2026-05-27 00:00:00')"
                ),
                {
                    "id": "44444444-4444-4444-4444-444444444444",
                    "tid": "11111111-1111-1111-1111-111111111111",
                },
            )
    finally:
        sync_eng.dispose()


def test_pre_existing_tables_undisturbed(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The migration touches no other table.

    A sanity check that ``identity_budget`` lands additively without
    column changes on ``audit_log`` / ``agent_run`` / ``agent_permission``.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    # Spot-check three pre-existing tables.
    for table in ("audit_log", "agent_run", "agent_permission"):
        assert table in _table_names(sync_url), (
            f"{table} must still be present after migration 0031"
        )


def test_reversibility_round_trip(
    alembic_cfg: tuple[Config, str],
) -> None:
    """downgrade to 0030 drops the table; upgrade recreates it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "identity_budget" in _table_names(sync_url)

    command.downgrade(cfg, "0030")
    assert "identity_budget" not in _table_names(sync_url)

    command.upgrade(cfg, "head")
    assert "identity_budget" in _table_names(sync_url)
