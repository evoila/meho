# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0116_add_approval_request_withdrawn`` (#4987).

The migration adds the ``withdrawn`` status and the ``withdrawn_at`` /
``withdraw_reason`` columns to ``approval_request``. The tests check that
the wider status rule takes ``withdrawn`` and still refuses other values,
that the SQLite table rebuild keeps the partial unique index, and that
``downgrade`` refuses while a withdrawn row exists. Every step targets
this revision and its ``down_revision``, never ``head``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.settings import get_settings

_REVISION = "0116"
_DOWN_REVISION = "0115"
_TABLE = "approval_request"
_TENANT = "00000000-0000-0000-0000-000000004987"
_VERSIONS = Path(__file__).resolve().parent.parent.parent / "alembic" / "versions"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0116.db"
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


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_migration_{filename[:4]}", _VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(sync_url: str, *, row_id: str, status: str, key: str | None = None) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO approval_request (id, tenant_id, principal_sub, op_id, "
                    "connector_id, params_hash, proposed_effect, status, created_at, "
                    "idempotency_key) VALUES (:id, :tenant, 'addon', 'op', 'c-1', 'h', '{}', "
                    ":status, '2026-10-26 00:00:00', :key)"
                ),
                {"id": row_id, "tenant": _TENANT, "status": status, "key": key},
            )
    finally:
        eng.dispose()


def _columns(sync_url: str) -> dict[str, dict[str, object]]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {c["name"]: dict(c) for c in inspect(conn).get_columns(_TABLE)}
    finally:
        eng.dispose()


def test_status_check_matches_enum() -> None:
    """The newest status tuple equals :class:`ApprovalRequestStatus` (drift guard)."""
    migration = _load("0116_add_approval_request_withdrawn.py")
    assert set(migration._STATUSES_WITH_WITHDRAWN) == {s.value for s in ApprovalRequestStatus}


def test_before_tuple_matches_migration_0023() -> None:
    """``downgrade`` narrows back to exactly the values migration 0023 allowed."""
    old = _load("0023_create_approval_request.py")
    new = _load("0116_add_approval_request_withdrawn.py")
    assert set(new._STATUSES_BEFORE) == set(old._APPROVAL_REQUEST_STATUSES)


def test_upgrade_takes_withdrawn_and_adds_nullable_columns(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _columns(sync_url)
    assert columns["withdrawn_at"]["nullable"] is True
    assert columns["withdraw_reason"]["nullable"] is True
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000b001", status="withdrawn")
    with pytest.raises(sa.exc.IntegrityError):
        _insert(sync_url, row_id="00000000-0000-0000-0000-00000000b002", status="cancelled")


def test_table_rebuild_keeps_the_partial_unique_index(alembic_cfg: tuple[Config, str]) -> None:
    """A live key still blocks a second row; a withdrawn row frees the key."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c001", status="pending", key="k")
    with pytest.raises(sa.exc.IntegrityError):
        _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c002", status="pending", key="k")
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text("UPDATE approval_request SET status = 'withdrawn'"))
    finally:
        eng.dispose()
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000c003", status="pending", key="k")


def test_downgrade_refuses_while_a_withdrawn_row_exists(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _insert(sync_url, row_id="00000000-0000-0000-0000-00000000d001", status="withdrawn")

    with pytest.raises(RuntimeError, match="withdrawn"):
        command.downgrade(cfg, _DOWN_REVISION)


def test_clean_downgrade_narrows_and_drops_then_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    columns = _columns(sync_url)
    assert "withdrawn_at" not in columns
    assert "withdraw_reason" not in columns
    with pytest.raises(sa.exc.IntegrityError):
        _insert(sync_url, row_id="00000000-0000-0000-0000-00000000e001", status="withdrawn")

    command.upgrade(cfg, _REVISION)
    assert "withdrawn_at" in _columns(sync_url)
