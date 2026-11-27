# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0190_add_endpoint_descriptor_hard_to_undo``.

The migration adds ``hard_to_undo`` (with a CHECK on its three words) and
``changes_rights`` to ``endpoint_descriptor``. The tests check that
``upgrade`` adds them and keeps an old row and the table's indexes, that the
CHECK refuses an unknown word, that ``changes_rights`` reads back true and
false, that ``downgrade`` drops them, and that the pair round-trips. SQLite is the test driver.
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
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0190"
_DOWN_REVISION = "0189"
_CHECK = "ck_endpoint_descriptor_hard_to_undo"
_COLUMNS = {"hard_to_undo", "changes_rights"}

_ROW_INSERT = (
    "INSERT INTO endpoint_descriptor (id, product, version, impl_id, op_id, source_kind, "
    "handler_ref, tags, parameter_schema, safety_level, requires_approval, is_enabled, "
    "needs_reingest, created_at, updated_at{extra_columns}) VALUES ('{id}', 'example', '1.0', "
    "'example', '{op_id}', 'typed', 'example.handler', '[]', '{{}}', 'caution', 0, 1, 0, "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP{extra_values})"
)


def _insert(op_id: str, *, hard_to_undo: str | None = None) -> str:
    extra_columns = ", hard_to_undo" if hard_to_undo is not None else ""
    extra_values = f", '{hard_to_undo}'" if hard_to_undo is not None else ""
    return _ROW_INSERT.format(
        id=uuid.uuid4().hex,
        op_id=op_id,
        extra_columns=extra_columns,
        extra_values=extra_values,
    )


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0190.db"
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


def _shape(sync_url: str) -> tuple[set[str], set[str], set[str]]:
    """Return the columns, the CHECK names and the index names of the table."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            columns = {str(col["name"]) for col in inspector.get_columns("endpoint_descriptor")}
            checks = {
                str(c["name"]) for c in inspector.get_check_constraints("endpoint_descriptor")
            }
            indexes = {str(i["name"]) for i in inspector.get_indexes("endpoint_descriptor")}
    finally:
        sync_eng.dispose()
    return columns, checks, indexes


def _run(sync_url: str, statement: str) -> list[tuple[object, ...]]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            result = conn.execute(text(statement))
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        sync_eng.dispose()


def test_upgrade_adds_both_columns_and_keeps_old_rows_and_indexes(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _, checks_before, indexes_before = _shape(sync_url)
    _run(sync_url, _insert("example.widget.old"))

    command.upgrade(cfg, _REVISION)

    columns, checks, indexes = _shape(sync_url)
    assert columns >= _COLUMNS
    assert checks == checks_before | {_CHECK}
    assert indexes == indexes_before
    rows = _run(sync_url, "SELECT op_id, hard_to_undo, changes_rights FROM endpoint_descriptor")
    assert rows == [("example.widget.old", None, None)]


def test_check_allows_the_three_words_and_refuses_any_other(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    for word in ("deletes", "stops", "overwrites"):
        _run(sync_url, _insert(f"example.widget.{word}", hard_to_undo=word))
    with pytest.raises(sa.exc.IntegrityError):
        _run(sync_url, _insert("example.widget.bad", hard_to_undo="removes"))


def test_changes_rights_reads_back_true_and_false(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    _run(sync_url, _insert("example.widget.grant"))
    _run(sync_url, _insert("example.widget.view"))
    _run(sync_url, "UPDATE endpoint_descriptor SET changes_rights = 1 WHERE op_id LIKE '%grant'")
    _run(sync_url, "UPDATE endpoint_descriptor SET changes_rights = 0 WHERE op_id LIKE '%view'")
    rows = _run(sync_url, "SELECT op_id, changes_rights FROM endpoint_descriptor ORDER BY op_id")
    assert [(op_id, bool(value)) for op_id, value in rows] == [
        ("example.widget.grant", True),
        ("example.widget.view", False),
    ]


def test_downgrade_drops_both_columns_and_the_check(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    _run(sync_url, _insert("example.widget.delete", hard_to_undo="deletes"))

    command.downgrade(cfg, _DOWN_REVISION)

    columns, checks, _ = _shape(sync_url)
    assert not (_COLUMNS & columns)
    assert _CHECK not in checks
    assert "ck_endpoint_descriptor_safety_level" in checks
    assert _run(sync_url, "SELECT op_id FROM endpoint_descriptor") == [("example.widget.delete",)]


def test_upgrade_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    columns, checks, _ = _shape(sync_url)
    assert columns >= _COLUMNS
    assert _CHECK in checks
