# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0159_add_policy_classification``.

The migration only adds: ``operation_policy_override.category`` and
``.labels`` (an old row reads ``NULL`` in both) and the ``tenant_policy``
table (at most one row for the whole install, one per tenant).
``downgrade()`` removes them again and keeps the override rows.

Every step targets this migration's own revision (``0159``) and its
``down_revision`` (``0158``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0159"
_DOWN_REVISION = "0158"
_OVERRIDE_ID = "a" * 32
_TENANT_ID = "b" * 32


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0159.db"
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


def _override_columns(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        inspector = inspect(engine)
        return {c["name"] for c in inspector.get_columns("operation_policy_override")}
    finally:
        engine.dispose()


def _tables(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _run(sync_url: str, sql: str, **params: str | None) -> list[tuple[object, ...]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def _insert_override(sync_url: str) -> None:
    _run(
        sync_url,
        "INSERT INTO operation_policy_override (id, tenant_id, product, version, impl_id, "
        "op_id, safety_level, set_by_sub, set_at) VALUES (:id, NULL, 'p', '1.x', 'p', "
        "'p.thing.get', 'dangerous', 'fixture', '2026-11-11 00:00:00')",
        id=_OVERRIDE_ID,
    )


def _insert_policy(sync_url: str, policy_id: str, tenant_id: str | None) -> None:
    _run(
        sync_url,
        "INSERT INTO tenant_policy (id, tenant_id, document, set_by_sub, set_at) "
        "VALUES (:id, :tenant, '{}', 'fixture', '2026-11-11 00:00:00')",
        id=policy_id,
        tenant=tenant_id,
    )


def test_an_old_override_reads_null(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_override(sync_url)

    command.upgrade(cfg, _REVISION)

    rows = _run(sync_url, "SELECT category, labels, safety_level FROM operation_policy_override")
    assert rows == [(None, None, "dangerous")]


def test_one_install_row_and_one_row_per_tenant(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    _insert_policy(sync_url, "1" * 32, None)
    _insert_policy(sync_url, "2" * 32, _TENANT_ID)
    with pytest.raises(IntegrityError):
        _insert_policy(sync_url, "3" * 32, None)
    with pytest.raises(IntegrityError):
        _insert_policy(sync_url, "4" * 32, _TENANT_ID)


def test_up_and_down_keep_the_override_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_override(sync_url)

    command.upgrade(cfg, _REVISION)
    assert {"category", "labels"} <= _override_columns(sync_url)
    assert "tenant_policy" in _tables(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert not {"category", "labels"} & _override_columns(sync_url)
    assert "tenant_policy" not in _tables(sync_url)
    assert _run(sync_url, "SELECT safety_level FROM operation_policy_override") == [("dangerous",)]
