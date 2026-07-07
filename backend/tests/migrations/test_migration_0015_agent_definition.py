# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0015_create_agent_definition``.

Initiative #802 (G11.1 agent runtime), Task #809 (T2). The migration is
the schema substrate of the agent-definition CRUD: it creates the
``agent_definition`` table + its unique ``(tenant_id, name)`` index,
mirroring the dedicated-table FK discipline migration 0008
(``broadcast_override``) established.

Test matrix
-----------

* **Upgrade adds the table + index.** ``upgrade head`` from a clean DB
  leaves ``agent_definition`` present with the full column set and the
  named ``agent_definition_tenant_name_idx`` index defined.
* **Reversibility round-trip.** ``downgrade "0014"`` (0015's
  ``down_revision``) drops the table + index; a subsequent
  ``upgrade head`` re-creates them. The target is the explicit revision
  rather than head-relative ``"-1"`` so the test keeps reverting *0015*
  even once a later migration becomes head.
* **Earlier schema survives.** ``broadcast_override`` (0008) and
  ``audit_log`` (0001) survive the upgrade unchanged.
* **ORM-field smoke.** :attr:`AgentDefinition` resolves on the mapped
  class with the expected attributes.

The tests follow the synchronous pattern established by
:mod:`tests.test_migration_0014_agent_session_id`:
``alembic.command.upgrade`` calls ``asyncio.run`` internally via
env.py's async cookbook, so the test function itself must be sync.
SQLite is the test driver; PG-side shape parity is covered by the
testcontainers replay suite in :mod:`tests.test_migration_rollback`.
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

_EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "name",
    "identity_ref",
    "model_tier",
    "system_prompt",
    "toolset",
    "turn_budget",
    "output_schema",
    "enabled",
    "created_by_sub",
    "created_at",
    "updated_at",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0015.db"
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
    """Return the set of table names in the SQLite DB."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _columns(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _indexes(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_table_and_index(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands the table with the full column set + index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "agent_definition" in _table_names(sync_url)
    assert _columns(sync_url, "agent_definition") == _EXPECTED_COLUMNS
    assert "agent_definition_tenant_name_idx" in _indexes(sync_url, "agent_definition")


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0014"`` drops the table + index; ``upgrade head`` restores."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "agent_definition" in _table_names(sync_url)

    command.downgrade(cfg, "0014")
    assert "agent_definition" not in _table_names(sync_url)

    command.upgrade(cfg, "head")
    assert "agent_definition" in _table_names(sync_url)
    assert "agent_definition_tenant_name_idx" in _indexes(sync_url, "agent_definition")


def test_earlier_schema_survives(alembic_cfg: tuple[Config, str]) -> None:
    """0015 leaves the broadcast_override (0008) + audit_log (0001) tables intact."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    tables = _table_names(sync_url)
    assert "broadcast_override" in tables
    assert "audit_log" in tables
    assert "agent_definition" in tables


def test_orm_field_resolves() -> None:
    """:attr:`AgentDefinition` resolves on the mapped class with key attrs."""
    from meho_backplane.db.models import AgentDefinition

    assert AgentDefinition.__tablename__ == "agent_definition"
    for attr in ("name", "identity_ref", "model_tier", "system_prompt", "turn_budget"):
        assert hasattr(AgentDefinition, attr)
