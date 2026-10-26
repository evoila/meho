# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0111_add_topology_scope_cutover_state``.

#3749 adds the two tables the staged scope cut-over needs and nothing
else. Coverage (SQLite driver; the cut-over itself is PostgreSQL-only
and lives in ``tests/integration/test_topology_scope_cutover.py``):

* **Upgrade** -- ``topology_identity_state`` holds one ``legacy`` row;
  ``topology_scope_manifest`` exists and is empty; ``graph_node`` keeps
  its old unique key.
* **CHECKs** -- a second state row and an unknown mode or manifest
  status are rejected.
* **Downgrade** -- drops both tables while the graph is ``legacy``;
  refuses once the mode is ``scoped`` (the rollback guard).

Every step targets this migration's own revision (``0111``) and its
``down_revision`` (``0110``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0111"
_DOWN_REVISION = "0110"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0111.db"
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


def _execute(sync_url: str, sql: str) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(sql))
    finally:
        eng.dispose()


def _fetch(sync_url: str, sql: str) -> list[Any]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return list(conn.execute(text(sql)).all())
    finally:
        eng.dispose()


def _tables(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_upgrade_seeds_one_legacy_state_row(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _fetch(sync_url, "SELECT id, mode, manifest_id FROM topology_identity_state") == [
        (1, "legacy", None)
    ]
    assert _fetch(sync_url, "SELECT count(*) FROM topology_scope_manifest") == [(0,)]
    eng = sa_create_engine(sync_url)
    try:
        indexes = {i["name"]: i for i in inspect(eng).get_indexes("graph_node")}
    finally:
        eng.dispose()
    assert indexes["graph_node_tenant_kind_name_idx"]["unique"], "the old key stays"


def test_checks_reject_a_second_row_and_unknown_values(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    for sql in (
        "INSERT INTO topology_identity_state (id, mode, changed_at) "
        "VALUES (2, 'legacy', CURRENT_TIMESTAMP)",
        "UPDATE topology_identity_state SET mode = 'half-scoped'",
        "INSERT INTO topology_scope_manifest (id, status, snapshot_version, body, "
        "created_by, created_at) VALUES ('0f8fad5bd9cb469fa16570867728950e', 'merged', "
        "'x', '{}', 'test', CURRENT_TIMESTAMP)",
    ):
        with pytest.raises(IntegrityError):
            _execute(sync_url, sql)


def test_downgrade_drops_both_tables_while_legacy(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    assert {"topology_identity_state", "topology_scope_manifest"}.isdisjoint(_tables(sync_url))
    command.upgrade(cfg, _REVISION)
    assert _fetch(sync_url, "SELECT mode FROM topology_identity_state") == [("legacy",)]


def test_downgrade_refuses_once_scoped(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _execute(sync_url, "UPDATE topology_identity_state SET mode = 'scoped'")

    with pytest.raises(RuntimeError, match="Refusing to downgrade 0111"):
        command.downgrade(cfg, _DOWN_REVISION)
    assert {"topology_identity_state", "topology_scope_manifest"} <= _tables(sync_url)
