# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0020_create_approval_request``.

Initiative #803 (G11.2 Agent permission model), Task #817 (T4). The
migration adds the ``approval_request`` table — one row per
``requires_approval`` dispatch that the policy gate parks durably.

Test matrix
-----------

* **Upgrade creates the table + columns + indexes.** ``upgrade head``
  from a clean DB leaves ``approval_request`` present with every
  documented column and its three named indexes.
* **Reversibility round-trip.** ``downgrade "0017"`` (0020's
  ``down_revision``) drops the table; a subsequent ``upgrade head``
  re-creates it.
* **audit_log untouched.** The migration must not disturb the
  pre-existing ``audit_log`` table.
* **Column nullability.** The nullable columns (``run_id`` /
  ``principal_act`` / ``target_id`` / ``reviewed_by`` / ``decided_at``
  / ``expires_at``) permit NULL; the NOT NULL columns (``tenant_id`` /
  ``principal_sub`` / ``op_id`` / ``connector_id`` / ``params_hash`` /
  ``proposed_effect`` / ``status`` / ``created_at``) reject it.
* **Status check constraint matches the ORM enum.**

The tests follow the synchronous pattern of
:mod:`tests.test_migration_0017_agent_run`.
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
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0020.db"
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


def test_upgrade_creates_approval_request_table(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` creates the ``approval_request`` table."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "approval_request" in _table_names(sync_url)


def test_upgrade_creates_expected_columns(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``approval_request`` has every documented column after upgrade."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    cols = _table_columns(sync_url, "approval_request")
    expected = {
        "id",
        "tenant_id",
        "run_id",
        "principal_sub",
        "principal_act",
        "op_id",
        "connector_id",
        "target_id",
        "params_hash",
        "proposed_effect",
        "status",
        "reviewed_by",
        "decided_at",
        "created_at",
        "expires_at",
    }
    assert expected <= cols


def test_upgrade_creates_named_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Three named indexes exist after upgrade."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    idx = _table_indexes(sync_url, "approval_request")
    assert "approval_request_tenant_created_at_idx" in idx
    assert "approval_request_status_idx" in idx
    assert "approval_request_run_id_idx" in idx


def test_reversibility_round_trip(
    alembic_cfg: tuple[Config, str],
) -> None:
    """downgrade to 0017 drops the table; upgrade recreates it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "approval_request" in _table_names(sync_url)

    command.downgrade(cfg, "0017")
    assert "approval_request" not in _table_names(sync_url)

    command.upgrade(cfg, "head")
    assert "approval_request" in _table_names(sync_url)


def test_audit_log_untouched_after_upgrade(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The migration must not disturb the pre-existing ``audit_log`` table."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    tables = _table_names(sync_url)
    assert "audit_log" in tables
    # The 0014 column must still be present.
    cols = _table_columns(sync_url, "audit_log")
    assert "agent_session_id" in cols


def test_nullable_columns_permit_null(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Nullable columns permit NULL; NOT NULL columns reject it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    nullable = ("run_id", "principal_act", "target_id", "reviewed_by", "decided_at", "expires_at")
    not_null = (
        "id",
        "tenant_id",
        "principal_sub",
        "op_id",
        "connector_id",
        "params_hash",
        "proposed_effect",
        "status",
        "created_at",
    )
    for col in nullable:
        assert _column_is_nullable(sync_url, "approval_request", col), (
            f"expected {col!r} to be nullable"
        )
    for col in not_null:
        assert not _column_is_nullable(sync_url, "approval_request", col), (
            f"expected {col!r} to be NOT NULL"
        )


def _load_migration_0020() -> object:
    """Load migration ``0020`` as a module via its file path.

    Alembic version files are digit-prefixed and not importable as normal
    dotted modules. Loading by file path with :mod:`importlib.util` is the
    robust way to reach the migration's recorded literal tuples.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0020_create_approval_request.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0020", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_status_check_matches_enum() -> None:
    """The migration's status vocabulary matches :class:`ApprovalRequestStatus`.

    Drift guard: if a new member is added to the enum without a migration
    update, this test catches the mismatch.
    """
    migration = _load_migration_0020()

    model_values = {s.value for s in ApprovalRequestStatus}
    migration_values = set(migration._APPROVAL_REQUEST_STATUSES)  # type: ignore[attr-defined]
    assert model_values == migration_values
