# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0056_create_spec_provenance``.

Initiative #2270 (ingest spec hygiene), Task #2291. Creates the
``spec_provenance`` table — one durable, non-spoofable provenance row
per accepted spec ingest (sha256 over raw bytes, fetched/inline/shipped
origin, operator, timestamp) keyed on the connector triple + audit uri
within a tenant scope.

Asserts the table + its columns land, both partial unique indexes exist,
the ``origin`` CHECK constraint rejects an out-of-vocabulary value, and
the migration round-trips (downgrade drops it, re-upgrade recreates it).

**Idempotency pinning (0049/0050/0053/0055 footgun).** Every forward /
round-trip step targets this migration's **own** revision (``0056``) and
its ``down_revision`` (``0055``), never ``head`` — so a future head
migration cannot make ``upgrade("head")`` re-run this ``create_table`` on
a schema that already has it (the "table already exists" stamp-replay
footgun). SQLite is the test driver and the migration uses only generic
DDL + partial indexes (SQLite 3.8.0+), so PG parity holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0056"
_DOWN_REVISION = "0055"
_TABLE = "spec_provenance"
_EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "product",
    "version",
    "impl_id",
    "uri",
    "sha256",
    "origin",
    "operator_sub",
    "ingested_at",
}
_EXPECTED_INDEXES = {"spec_provenance_global_idx", "spec_provenance_tenant_idx"}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0056.db"
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


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({_TABLE})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _index_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def test_upgrade_creates_table_columns_and_indexes(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0056`` creates ``spec_provenance`` with its columns + indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _TABLE in _table_names(sync_url)
    assert _columns(sync_url) >= _EXPECTED_COLUMNS
    assert _index_names(sync_url) >= _EXPECTED_INDEXES


def test_origin_check_constraint_rejects_unknown_value(alembic_cfg: tuple[Config, str]) -> None:
    """The ``origin`` CHECK constraint bounds the enum to the three known values."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO spec_provenance "
                        "(id, product, version, impl_id, uri, sha256, origin, ingested_at) "
                        "VALUES ('id-1', 'p', 'v', 'i', 'u', 'sha', 'bogus', '2026-07-10')"
                    )
                )
    finally:
        sync_eng.dispose()


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0055"`` drops the table; ``upgrade "0056"`` recreates it.

    Pinned to this migration's own revision on both legs (never ``head``)
    so a future head migration cannot break the round-trip.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _table_names(sync_url), "downgrade must drop spec_provenance"
    assert not (_EXPECTED_INDEXES & _index_names(sync_url)), (
        "downgrade must drop the partial unique indexes too"
    )

    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)
