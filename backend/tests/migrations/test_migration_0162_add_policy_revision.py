# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0162_add_policy_revision``.

The migration only adds the ``policy_revision`` table; ``downgrade()``
removes it again. Every step targets this migration's own revision
(``0162``) and its ``down_revision`` (``0161``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0162"
_DOWN_REVISION = "0161"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0162.db"
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


def _tables(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_up_adds_the_table_and_down_drops_it(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    assert "policy_revision" not in _tables(sync_url)

    command.upgrade(cfg, _REVISION)
    assert "policy_revision" in _tables(sync_url)
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_revision (id, tenant_id, ts, imported_by_sub, "
                    "approved_by_sub, file_sha256, file_text, document, diff) VALUES "
                    "(:id, NULL, '2026-11-11 00:00:00', 'a', 'b', 'c', 'policy: v1', '{}', '{}')"
                ),
                {"id": "1" * 32},
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, _DOWN_REVISION)
    assert "policy_revision" not in _tables(sync_url)
