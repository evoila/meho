# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0191_add_audit_log_caller_kind``.

The migration adds the nullable column ``audit_log.caller_kind`` with a
CHECK that allows only the five kinds of ``CALLER_KINDS``. The tests check
that an old row keeps ``NULL``, that every index of the table stays (also
the expression index of migration ``0179``), that the CHECK refuses any
other value, that ``downgrade`` drops the column, and that the migration,
the model and ``CALLER_KINDS`` name the same five kinds. SQLite is the test
driver; ``test_migration_0191_audit_log_caller_kind_postgres.py`` runs the
PostgreSQL path.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.auth.caller_kind import CALLER_KINDS
from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings

_REVISION = "0191"
_DOWN_REVISION = "0190"
_CHECK = "ck_audit_log_caller_kind"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0191.db"
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


def _run(sync_url: str, statement: str, **params: object) -> list[tuple[object, ...]]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            result = conn.execute(text(statement), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        sync_eng.dispose()


def _insert(sync_url: str, *, caller_kind: str | None = None) -> str:
    """Insert one audit row; set ``caller_kind`` only when given. Return its id."""
    row_id = uuid.uuid4().hex
    extra_column = ", caller_kind" if caller_kind is not None else ""
    extra_value = ", :caller_kind" if caller_kind is not None else ""
    _run(
        sync_url,
        "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, "
        f"payload{extra_column}) VALUES (:id, '2026-01-01 00:00:00', 'op', 'DISPATCH', "
        f"'test.op', 200, '{{}}'{extra_value})",
        id=row_id,
        caller_kind=caller_kind,
    )
    return row_id


def _columns(sync_url: str) -> set[str]:
    return {str(row[1]) for row in _run(sync_url, "PRAGMA table_info(audit_log)")}


def _index_names(sync_url: str) -> set[str]:
    """Every index of ``audit_log``, read from ``sqlite_master``.

    SQLAlchemy's inspector skips an expression index on SQLite, so it would
    not see the index of migration ``0179``.
    """
    rows = _run(
        sync_url,
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'audit_log'",
    )
    return {str(row[0]) for row in rows}


def _kinds_in(condition: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", condition))


def test_upgrade_keeps_old_rows_null_and_every_index(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    old_id = _insert(sync_url)
    indexes_before = _index_names(sync_url)
    assert "audit_log_operator_sub_client_azp_occurred_at_idx" in indexes_before

    command.upgrade(cfg, _REVISION)

    assert "caller_kind" in _columns(sync_url)
    assert _index_names(sync_url) == indexes_before
    rows = _run(sync_url, "SELECT caller_kind FROM audit_log WHERE id = :id", id=old_id)
    assert rows == [(None,)]


def test_check_allows_the_five_kinds_and_refuses_any_other(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    for kind in CALLER_KINDS:
        _insert(sync_url, caller_kind=kind)
    _insert(sync_url)
    stored = _run(sync_url, "SELECT caller_kind FROM audit_log")
    assert sorted(str(row[0]) for row in stored) == sorted([*CALLER_KINDS, "None"])

    for bad in ("agent", "Person", "", "system "):
        with pytest.raises(sa.exc.IntegrityError, match=_CHECK):
            _insert(sync_url, caller_kind=bad)


def test_downgrade_drops_the_column_and_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    row_id = _insert(sync_url, caller_kind="runner")
    indexes = _index_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert "caller_kind" not in _columns(sync_url)
    assert _index_names(sync_url) == indexes

    command.upgrade(cfg, _REVISION)
    rows = _run(sync_url, "SELECT caller_kind FROM audit_log WHERE id = :id", id=row_id)
    assert rows == [(None,)]
    with pytest.raises(sa.exc.IntegrityError, match=_CHECK):
        _insert(sync_url, caller_kind="bogus")


def test_migration_and_model_name_the_kinds_of_caller_kinds() -> None:
    """The kinds are written out twice; both copies must match ``CALLER_KINDS``."""
    script = ScriptDirectory.from_config(alembic_config())
    migration = script.get_revision(_REVISION)
    assert migration is not None
    migration_condition = str(migration.module._CONDITION)

    model_checks = [
        c
        for c in AuditLog.__table__.constraints
        if isinstance(c, sa.CheckConstraint) and c.name == _CHECK
    ]
    assert len(model_checks) == 1
    model_condition = str(model_checks[0].sqltext)

    assert model_condition == migration_condition
    assert _kinds_in(migration_condition) == set(CALLER_KINDS)
    assert "caller_kind IS NULL" in migration_condition
