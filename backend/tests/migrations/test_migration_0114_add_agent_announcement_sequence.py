# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0114_add_agent_announcement_sequence``.

#3748 adds the per-tenant counter table ``agent_announcement_sequence``,
the nullable ``agent_announcement.seq`` column and a unique
``(tenant_id, seq)`` index. Existing rows get numbers 1..n per tenant,
oldest first, and each tenant's counter starts at its highest number.
``downgrade`` removes all of it again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0114"
_DOWN_REVISION = "0113"
_TABLE = "agent_announcement"
_COUNTER = "agent_announcement_sequence"
_INDEX = "agent_announcement_tenant_seq_uq"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0114.db"
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


def _insert(conn: sa.Connection, *, tenant_id: str, created_at: str, activity: str) -> None:
    conn.execute(
        sa.text(
            f"INSERT INTO {_TABLE} (id, tenant_id, principal_sub, activity, targets, phase, "
            "created_at) VALUES (:id, :tenant_id, 'svc-1', :activity, '[]', 'start', :created_at)"
        ),
        {
            "id": uuid.uuid4().hex,
            "tenant_id": tenant_id,
            "activity": activity,
            "created_at": created_at,
        },
    )


def test_upgrade_numbers_existing_rows_per_tenant(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    tenant_a, tenant_b = uuid.uuid4().hex, uuid.uuid4().hex
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            _insert(conn, tenant_id=tenant_a, created_at="2026-10-16 10:02:00", activity="a2")
            _insert(conn, tenant_id=tenant_b, created_at="2026-10-16 10:00:00", activity="b1")
            _insert(conn, tenant_id=tenant_a, created_at="2026-10-16 10:00:00", activity="a1")
            _insert(conn, tenant_id=tenant_a, created_at="2026-10-16 10:05:00", activity="a3")

        command.upgrade(cfg, _REVISION)

        with sync_eng.connect() as conn:
            numbered = conn.execute(
                sa.text(f"SELECT tenant_id, activity, seq FROM {_TABLE} ORDER BY tenant_id, seq")
            ).all()
            counters = dict(
                conn.execute(sa.text(f"SELECT tenant_id, last_seq FROM {_COUNTER}")).all()
            )
        assert {(row.activity, row.seq) for row in numbered} == {
            ("a1", 1),
            ("a2", 2),
            ("a3", 3),
            ("b1", 1),
        }
        assert counters == {tenant_a: 3, tenant_b: 1}

        with pytest.raises(sa.exc.IntegrityError), sync_eng.begin() as conn:
            conn.execute(sa.text(f"UPDATE {_TABLE} SET seq = 1 WHERE activity = 'a2'"))
    finally:
        sync_eng.dispose()

    inspector_eng = sa_create_engine(sync_url)
    try:
        with inspector_eng.connect() as conn:
            inspector = inspect(conn)
            seq_column = {c["name"]: c for c in inspector.get_columns(_TABLE)}["seq"]
            assert seq_column["nullable"] is True
            assert _INDEX in {i["name"] for i in inspector.get_indexes(_TABLE)}
            assert _COUNTER in inspector.get_table_names()
    finally:
        inspector_eng.dispose()


def test_downgrade_removes_counter_column_and_index(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            assert "seq" not in {c["name"] for c in inspector.get_columns(_TABLE)}
            assert _INDEX not in {i["name"] for i in inspector.get_indexes(_TABLE)}
            assert _COUNTER not in inspector.get_table_names()
    finally:
        sync_eng.dispose()

    command.upgrade(cfg, _REVISION)
