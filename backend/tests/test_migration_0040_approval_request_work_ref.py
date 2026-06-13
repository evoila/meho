# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0040_add_approval_request_work_ref``.

Initiative #1653, Task #1659 (work_ref I2-T1). Adds the nullable
``approval_request.work_ref`` column + its b-tree index -- the external
change-ticket reference recorded on a parked approval. Soft-column
discipline mirrors 0039 / 0017: nullable, no server default (Python-side
``None``), reversible.

Mirrors :mod:`tests.test_migration_0039_work_ref`; SQLite is the test
driver and the migration uses only generic DDL, so PG parity holds.
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


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0040.db"
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


def _approval_request_columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(approval_request)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _approval_request_column_is_nullable(sync_url: str, column: str) -> bool:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(approval_request)")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on approval_request")


def _approval_request_indexes(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA index_list(approval_request)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_work_ref_column_and_index(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands the nullable column + its named index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "work_ref" in _approval_request_columns(sync_url), (
        "migration 0040 must add approval_request.work_ref on upgrade head"
    )
    assert _approval_request_column_is_nullable(sync_url, "work_ref"), (
        "work_ref must be nullable -- NULL when no change ticket authorised the request"
    )
    assert "approval_request_work_ref_idx" in _approval_request_indexes(sync_url), (
        "migration 0040 must create approval_request_work_ref_idx on upgrade head"
    )


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0039"`` drops column + index; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "work_ref" in _approval_request_columns(sync_url)
    assert "approval_request_work_ref_idx" in _approval_request_indexes(sync_url)

    command.downgrade(cfg, "0039")
    assert "work_ref" not in _approval_request_columns(sync_url), "downgrade must drop work_ref"
    assert "approval_request_work_ref_idx" not in _approval_request_indexes(sync_url), (
        "downgrade must drop approval_request_work_ref_idx"
    )

    command.upgrade(cfg, "head")
    assert "work_ref" in _approval_request_columns(sync_url)
    assert "approval_request_work_ref_idx" in _approval_request_indexes(sync_url)


def test_prior_approval_request_columns_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """The pre-existing columns + indexes survive 0040."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _approval_request_columns(sync_url)
    assert "run_id" in columns, "the run_id soft-FK must survive 0040"
    assert "params" in columns, "0036's params column must survive 0040"
    assert "params_hash" in columns
    assert "work_ref" in columns

    indexes = _approval_request_indexes(sync_url)
    assert "approval_request_status_idx" in indexes
    assert "approval_request_run_id_idx" in indexes
    assert "approval_request_work_ref_idx" in indexes


def test_orm_field_resolves_and_defaults_none() -> None:
    """:attr:`ApprovalRequest.work_ref` resolves and defaults to ``None``."""
    import uuid

    from meho_backplane.db.models import ApprovalRequest

    assert hasattr(ApprovalRequest, "work_ref")
    row = ApprovalRequest(
        tenant_id=uuid.uuid4(),
        principal_sub="op-smoke",
        op_id="vault.kv.write",
        connector_id="vault-1.x",
        params_hash="deadbeef",
        proposed_effect={},
        status="pending",
    )
    assert row.work_ref is None, "an ApprovalRequest built without work_ref must default it to None"
