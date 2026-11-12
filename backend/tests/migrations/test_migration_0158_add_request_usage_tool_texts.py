# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0158_add_request_usage_tool_texts``.

The migration adds the nullable ``request_usage_event.tool_texts`` column
and a CHECK constraint that allows only ``short``. ``downgrade()`` drops
them. Every step targets this migration's own revision (``0158``) and its
``down_revision`` (``0157``), never ``head``.
"""

from __future__ import annotations

import uuid
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

_REVISION = "0158"
_DOWN_REVISION = "0157"
_TABLE = "request_usage_event"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0158.db"
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


def _columns(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(_TABLE)}
    finally:
        engine.dispose()


def _insert(sync_url: str, tool_texts: str | None) -> None:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {_TABLE} (id, kind, occurred_at, tenant_id, principal_sub,"
                    " transport, route, outcome, duration_ms, tool_texts) VALUES (:id,"
                    " 'request_finished', '2026-11-11 00:00:00', :tenant, 'op-1', 'mcp',"
                    " 'tool:meho_status', 'ok', 1.0, :tool_texts)"
                ),
                {"id": uuid.uuid4().hex, "tenant": uuid.uuid4().hex, "tool_texts": tool_texts},
            )
    finally:
        engine.dispose()


def test_up_adds_the_column_and_down_drops_it(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    assert "tool_texts" not in _columns(sync_url)

    command.upgrade(cfg, _REVISION)
    assert "tool_texts" in _columns(sync_url)
    _insert(sync_url, "short")
    _insert(sync_url, None)
    with pytest.raises(IntegrityError):
        _insert(sync_url, "full")

    command.downgrade(cfg, _DOWN_REVISION)
    assert "tool_texts" not in _columns(sync_url)

    command.upgrade(cfg, _REVISION)
    assert "tool_texts" in _columns(sync_url)
