# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0140_add_kb_revision_entry_metadata``.

The migration adds the nullable ``entry_metadata`` column to
``kb_revision``. The tests check that:

* the column does not exist before the migration and does after it;
* a revision row that exists before the migration keeps ``NULL``, and
  the column takes JSON afterwards;
* ``downgrade`` drops the column and keeps the rows.

Every step targets this revision and its ``down_revision``, never
``head``. SQLite is the test driver.
"""

from __future__ import annotations

import json
import uuid
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

_REVISION = "0140"
_DOWN_REVISION = "0139"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0140.db"
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


def _columns(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        return {col["name"] for col in inspect(eng).get_columns("kb_revision")}
    finally:
        eng.dispose()


def _insert_revision(sync_url: str, *, with_metadata: bool) -> str:
    """Insert a kb head and one revision; return the revision id (SQLite keeps no foreign keys)."""
    tenant_id = uuid.uuid4().hex
    entry_id = uuid.uuid4().hex
    revision_id = uuid.uuid4().hex
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, source, source_id, kind, body, "
                    "body_hash, embedding, metadata, created_at, updated_at) VALUES "
                    "(:id, :tenant, 'kb', 'slug-a', 'kb-entry', 'body', 'h', "
                    "'[]', '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                ),
                {"id": entry_id, "tenant": tenant_id},
            )
            columns = (
                "id, tenant_id, entry_id, revision_number, body, body_hash, origin, created_at"
            )
            values = ":id, :tenant, :entry, 1, 'body', 'h', 'write', '2026-01-01 00:00:00'"
            params: dict[str, str] = {"id": revision_id, "tenant": tenant_id, "entry": entry_id}
            if with_metadata:
                columns += ", entry_metadata"
                values += ", :meta"
                params["meta"] = json.dumps({"tag": "x"})
            conn.execute(text(f"INSERT INTO kb_revision ({columns}) VALUES ({values})"), params)
    finally:
        eng.dispose()
    return revision_id


def test_upgrade_adds_a_nullable_column_and_keeps_old_rows(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    assert "entry_metadata" not in _columns(sync_url)
    old_id = _insert_revision(sync_url, with_metadata=False)

    command.upgrade(cfg, _REVISION)

    assert "entry_metadata" in _columns(sync_url)
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            kept = conn.execute(
                text("SELECT entry_metadata FROM kb_revision WHERE id = :id"), {"id": old_id}
            ).scalar_one()
    finally:
        eng.dispose()
    assert kept is None
    _insert_revision(sync_url, with_metadata=True)


def test_downgrade_drops_the_column_and_keeps_the_rows(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    revision_id = _insert_revision(sync_url, with_metadata=True)

    command.downgrade(cfg, _DOWN_REVISION)

    assert "entry_metadata" not in _columns(sync_url)
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM kb_revision WHERE id = :id"), {"id": revision_id}
            ).scalar_one()
    finally:
        eng.dispose()
    assert count == 1
