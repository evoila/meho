# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0137_add_audit_log_config_change_class`` (#5791).

The migration adds ``config_change`` to the audit event class CHECK. The
tests check that:

* before it the database refuses ``config_change``, after it the database
  takes it, and an old row keeps its class;
* the database still refuses a class that is not in the list;
* the migration's list is the model's list;
* ``downgrade`` refuses while a ``config_change`` row exists, and
  round-trips when none does.

Every step targets this revision and its ``down_revision``, never
``head``. SQLite is the test driver; the migration uses
``batch_alter_table``, so the same DDL runs on PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AUDIT_EVENT_CLASSES
from meho_backplane.settings import get_settings

_REVISION = "0137"
_DOWN_REVISION = "0136"
_VERSIONS = Path(__file__).resolve().parent.parent.parent / "alembic" / "versions"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0137.db"
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


def _insert(sync_url: str, event_class: str) -> str:
    """Insert one audit row of *event_class*; return its id."""
    row_id = uuid.uuid4().hex
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, "
                    "status_code, payload, event_class) VALUES (:id, "
                    "'2026-01-01 00:00:00', 'op', 'SERVICE', 'meho_connector_edit_op', "
                    "200, '{}', :event_class)"
                ),
                {"id": row_id, "event_class": event_class},
            )
    finally:
        eng.dispose()
    return row_id


def _event_class_of(sync_url: str, row_id: str) -> str:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT event_class FROM audit_log WHERE id = :id"), {"id": row_id}
                ).scalar_one()
            )
    finally:
        eng.dispose()


def test_upgrade_lets_config_change_in_and_keeps_old_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    old_id = _insert(sync_url, "operation_execution")
    with pytest.raises(IntegrityError, match="ck_audit_log_event_class"):
        _insert(sync_url, "config_change")

    command.upgrade(cfg, _REVISION)

    new_id = _insert(sync_url, "config_change")
    assert _event_class_of(sync_url, old_id) == "operation_execution"
    assert _event_class_of(sync_url, new_id) == "config_change"
    with pytest.raises(IntegrityError, match="ck_audit_log_event_class"):
        _insert(sync_url, "bogus")


def test_migration_list_is_the_model_list() -> None:
    spec = importlib.util.spec_from_file_location(
        "_migration_0137", _VERSIONS / "0137_add_audit_log_config_change_class.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module._CLASSES_WITH_CONFIG_CHANGE) == set(AUDIT_EVENT_CLASSES)


def test_downgrade_refuses_while_a_config_change_row_exists(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    _insert(sync_url, "config_change")

    with pytest.raises(RuntimeError, match="1 config_change row"):
        command.downgrade(cfg, _DOWN_REVISION)


def test_downgrade_round_trips_without_config_change_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    row_id = _insert(sync_url, "policy_decision")

    command.downgrade(cfg, _DOWN_REVISION)
    with pytest.raises(IntegrityError, match="ck_audit_log_event_class"):
        _insert(sync_url, "config_change")

    command.upgrade(cfg, _REVISION)
    assert _event_class_of(sync_url, row_id) == "policy_decision"
