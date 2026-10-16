# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0105_add_agent_announcement_claims``.

#3736 adds five nullable trusted-claim columns to ``agent_announcement``
and three partial unique indexes. The tests check that ``upgrade`` adds
them, that the indexes refuse a second ``completion`` for one claim and a
reused retry key for one owner, that a legacy row (all new columns NULL)
still inserts, and that ``downgrade`` removes everything again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
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

_REVISION = "0105"
_DOWN_REVISION = "0104"
_TABLE = "agent_announcement"
_COLUMNS = {"claim_id", "owner_sub", "actor_sub", "admission", "idempotency_key"}
_INDEXES = {
    "agent_announcement_idempotency_uq",
    "agent_announcement_claim_start_uq",
    "agent_announcement_claim_completion_uq",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0105.db"
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


def _schema(sync_url: str) -> tuple[dict[str, dict[str, object]], set[str]]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            columns = {c["name"]: c for c in inspector.get_columns(_TABLE)}
            indexes = {i["name"] for i in inspector.get_indexes(_TABLE) if i["name"]}
    finally:
        sync_eng.dispose()
    return columns, indexes


def _insert(conn: sa.Connection, **values: object) -> None:
    row: dict[str, object] = {
        "id": uuid.uuid4().hex,
        "tenant_id": values.pop("tenant_id"),
        "principal_sub": "svc-1",
        "activity": "x",
        "targets": "[]",
        "phase": "start",
        "created_at": "2026-10-16 10:00:00",
        **values,
    }
    names = ", ".join(row)
    params = ", ".join(f":{name}" for name in row)
    conn.execute(sa.text(f"INSERT INTO {_TABLE} ({names}) VALUES ({params})"), row)


def test_upgrade_adds_nullable_columns_and_indexes(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns, indexes = _schema(sync_url)
    assert set(columns) >= _COLUMNS
    assert all(columns[name]["nullable"] is True for name in _COLUMNS)
    assert indexes >= _INDEXES


def test_indexes_enforce_one_completion_and_one_key_per_owner(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant = uuid.uuid4().hex
    claim = uuid.uuid4().hex
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            # Legacy rows (all new columns NULL) keep inserting freely.
            _insert(conn, tenant_id=tenant)
            _insert(conn, tenant_id=tenant)
            _insert(conn, tenant_id=tenant, claim_id=claim, owner_sub="agent:a")
            _insert(
                conn,
                tenant_id=tenant,
                claim_id=claim,
                owner_sub="agent:a",
                phase="completion",
                idempotency_key="k1",
            )
        with pytest.raises(sa.exc.IntegrityError), sync_eng.begin() as conn:
            _insert(
                conn,
                tenant_id=tenant,
                claim_id=claim,
                owner_sub="agent:a",
                phase="completion",
                idempotency_key="k2",
            )
        with pytest.raises(sa.exc.IntegrityError), sync_eng.begin() as conn:
            _insert(conn, tenant_id=tenant, owner_sub="agent:a", idempotency_key="k1")
        with sync_eng.begin() as conn:
            # The same key for a different owner is fine.
            _insert(conn, tenant_id=tenant, owner_sub="agent:b", idempotency_key="k1")
    finally:
        sync_eng.dispose()


def test_downgrade_removes_columns_and_indexes(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    columns, indexes = _schema(sync_url)
    assert not _COLUMNS & set(columns)
    assert not _INDEXES & indexes

    command.upgrade(cfg, _REVISION)
    columns, _indexes = _schema(sync_url)
    assert set(columns) >= _COLUMNS
