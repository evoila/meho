# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0119_add_endpoint_descriptor_owner_package`` (#5037).

* The upgrade adds exactly one column, ``endpoint_descriptor.owner_package``,
  and it is nullable. No other table or column changes.
* An existing row reads ``NULL`` (built in) after the upgrade.
* The downgrade drops the column again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0119"
_DOWN_REVISION = "0118"

_OLD_ROW_INSERT = (
    "INSERT INTO endpoint_descriptor (id, product, version, impl_id, op_id, source_kind, "
    "handler_ref, tags, parameter_schema, safety_level, requires_approval, is_enabled, "
    "needs_reingest, created_at, updated_at) VALUES ('{id}', 'example', '1.0', 'example', "
    "'example.widget.list', 'typed', 'example.handler', '[]', '{{}}', 'safe', 0, 1, 0, "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0119.db"
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


def _schema(sync_url: str) -> dict[str, dict[str, bool]]:
    eng = sa_create_engine(sync_url)
    try:
        inspector = inspect(eng)
        return {
            table: {col["name"]: bool(col["nullable"]) for col in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }
    finally:
        eng.dispose()


def test_upgrade_adds_only_one_nullable_column(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    before = _schema(sync_url)
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(_OLD_ROW_INSERT.format(id="1" * 32)))
    finally:
        eng.dispose()

    command.upgrade(cfg, _REVISION)
    after = _schema(sync_url)

    assert set(after) == set(before)
    for table, columns in before.items():
        if table != "endpoint_descriptor":
            assert after[table] == columns, table
    added = set(after["endpoint_descriptor"]) - set(before["endpoint_descriptor"])
    assert added == {"owner_package"}
    assert after["endpoint_descriptor"]["owner_package"] is True

    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            owners = conn.execute(text("SELECT owner_package FROM endpoint_descriptor")).all()
    finally:
        eng.dispose()
    assert owners == [(None,)]


def test_downgrade_drops_the_column(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert "owner_package" not in _schema(sync_url)["endpoint_descriptor"]
