# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0123_add_operation_group_code_text_hash`` (#4332).

The migration adds ``operation_group.code_text_hash`` and fills it for
built-in rows that hold a group text MEHO shipped before. Every step
targets this revision and its ``down_revision``, never ``head``.
"""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0123"
_DOWN_REVISION = "0122"
_VERSIONS = Path(__file__).resolve().parent.parent.parent / "alembic" / "versions"
#: A text the first versions shipped for the vault ``kv`` group.
_SHIPPED_TEXT = "Operations grouped under 'kv' for vault vault."
_OPERATOR_TEXT = "Our own text for the kv group."
_TENANT = "00000000-0000-0000-0000-000000004332"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0123.db"
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


def _load(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_migration_{filename[:4]}", _VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _insert(sync_url: str, *, group_key: str, text: str, tenant_id: str | None = None) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO operation_group (id, tenant_id, product, version, impl_id, "
                    "group_key, name, when_to_use, review_status, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, 'vault', '1.x', 'vault', :group_key, 'Kv', "
                    ":text, 'enabled', '2026-10-29 00:00:00', '2026-10-29 00:00:00')"
                ),
                {"id": uuid4().hex, "tenant_id": tenant_id, "group_key": group_key, "text": text},
            )
    finally:
        eng.dispose()


def _rows(sync_url: str) -> dict[str, tuple[str, str | None]]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            result = conn.execute(
                sa.text("SELECT group_key, when_to_use, code_text_hash FROM operation_group")
            )
            return {key: (text, digest) for key, text, digest in result}
    finally:
        eng.dispose()


def _columns(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {c["name"] for c in sa.inspect(conn).get_columns("operation_group")}
    finally:
        eng.dispose()


def test_every_text_of_migration_0011_counts_as_shipped() -> None:
    old = _load("0011_backfill_operation_group_when_to_use.py")
    new = _load("0123_add_operation_group_code_text_hash.py")
    texts = set(old._CURATED_WHEN_TO_USE.values())
    assert texts
    assert {_sha(text) for text in texts} <= new._SHIPPED_TEXT_HASHES


def test_upgrade_fills_the_hash_only_for_built_in_rows_with_a_shipped_text(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert(sync_url, group_key="shipped", text=_SHIPPED_TEXT)
    _insert(sync_url, group_key="operator", text=_OPERATOR_TEXT)
    _insert(sync_url, group_key="tenant", text=_SHIPPED_TEXT, tenant_id=_TENANT)

    command.upgrade(cfg, _REVISION)

    assert _rows(sync_url) == {
        "shipped": (_SHIPPED_TEXT, _sha(_SHIPPED_TEXT)),
        "operator": (_OPERATOR_TEXT, None),
        "tenant": (_SHIPPED_TEXT, None),
    }


def test_downgrade_drops_the_column_and_keeps_the_texts(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert(sync_url, group_key="shipped", text=_SHIPPED_TEXT)
    command.upgrade(cfg, _REVISION)
    assert "code_text_hash" in _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)

    assert "code_text_hash" not in _columns(sync_url)
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            texts = conn.execute(sa.text("SELECT when_to_use FROM operation_group")).scalars()
            assert list(texts) == [_SHIPPED_TEXT]
    finally:
        eng.dispose()
    command.upgrade(cfg, _REVISION)
    assert "code_text_hash" in _columns(sync_url)
