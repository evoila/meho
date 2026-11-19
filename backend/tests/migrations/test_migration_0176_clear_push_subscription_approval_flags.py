# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0176_clear_push_subscription_approval_flags``.

The migration sets ``approver`` and ``self_approver`` to false on every
``push_subscription`` row and keeps the other login facts. A person gets
the flag back at the next sign-in, when their login still gives it.

Every step targets this migration's own revision (``0176``) and its
``down_revision`` (``0175``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0176"
_DOWN_REVISION = "0175"
_TENANT_ID = "b" * 32
_READ_AT = "2026-11-15 12:00:00.000000"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0176.db"
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


def _run(sync_url: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def _insert_row(
    sync_url: str, *, name: str, role: str, approver: bool, self_approver: bool
) -> None:
    _run(
        sync_url,
        "INSERT INTO push_subscription (id, operator_sub, tenant_id, endpoint, p256dh, auth, "
        "created_at, tenant_role, approver, self_approver, platform_admin, facts_read_at) "
        "VALUES (:id, :sub, :tenant, :endpoint, 'key', 'secret', :ts, :role, :approver, "
        ":self_approver, 1, :ts)",
        id=name.ljust(32, "0"),
        sub=name,
        tenant=_TENANT_ID,
        endpoint=f"https://push.example/{name}",
        ts=_READ_AT,
        role=role,
        approver=approver,
        self_approver=self_approver,
    )


def _facts(sync_url: str) -> list[tuple[object, ...]]:
    return _run(
        sync_url,
        "SELECT operator_sub, tenant_role, approver, self_approver, platform_admin, "
        "facts_read_at FROM push_subscription ORDER BY operator_sub",
    )


def test_the_approval_flags_are_cleared_and_the_other_facts_stay(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_row(sync_url, name="approver", role="read_only", approver=True, self_approver=False)
    _insert_row(sync_url, name="sa", role="operator", approver=False, self_approver=True)
    _insert_row(sync_url, name="reader", role="read_only", approver=False, self_approver=False)

    command.upgrade(cfg, _REVISION)

    assert _facts(sync_url) == [
        ("approver", "read_only", 0, 0, 1, _READ_AT),
        ("reader", "read_only", 0, 0, 1, _READ_AT),
        ("sa", "operator", 0, 0, 1, _READ_AT),
    ]


def test_up_and_down_keep_the_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_row(sync_url, name="approver", role="read_only", approver=True, self_approver=False)

    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert _run(sync_url, "SELECT operator_sub, approver FROM push_subscription") == [
        ("approver", 0)
    ]
