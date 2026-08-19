# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0073_event_outbox_dedupe_key_origin``.

Initiative #2877, Task #2879. The migration adds two additive nullable
columns to ``event_outbox`` (migration ``0027``) plus a partial unique
index:

* ``dedupe_key`` -- Text, nullable. Producer-supplied idempotency token
  for at-least-once external ingest.
* ``origin`` -- Text, nullable. Event provenance (NULL = internal MEHO
  producer, else the external event-source id).
* ``event_outbox_tenant_dedupe_idx`` -- ``UNIQUE (tenant_id, dedupe_key)
  WHERE dedupe_key IS NOT NULL``, emitted on both dialects.

Coverage (mapped to the issue's acceptance criteria):

* **Additive schema** -- ``upgrade 0073`` leaves both columns present and
  nullable, and the partial unique index in place.
* **SQLite partial-index parity** -- the index's stored DDL carries the
  ``UNIQUE ... WHERE dedupe_key IS NOT NULL`` predicate on SQLite, so the
  unit-test path enforces the same shape PostgreSQL does.
* **DB-enforced dedupe** -- a duplicate ``(tenant_id, dedupe_key)`` raises
  :class:`IntegrityError` (the #2881 ingest endpoint maps this to an
  idempotent ``200``).
* **Internal producers unaffected** -- many rows with ``dedupe_key`` NULL
  coexist in one tenant (the partial predicate excludes them), and the
  uniqueness is per tenant (two tenants may reuse the same key).
* **Reversibility round-trip** -- ``downgrade 0072`` drops the two columns
  and the dedupe index while preserving the two original indexes; a
  subsequent ``upgrade 0073`` restores the additive shape.

Every step targets this migration's own revision (``0073``) and its
``down_revision`` (``0072``), never ``head`` -- so a future head migration
cannot silently change what these tests exercise (the 0049/0054 footgun).
SQLite is the test driver; the migration emits ``sqlite_where`` so PG
parity holds via the equivalent native partial index.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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

_REVISION = "0073"
_DOWN_REVISION = "0072"

_NEW_COLUMNS: frozenset[str] = frozenset({"dedupe_key", "origin"})
_DEDUPE_INDEX = "event_outbox_tenant_dedupe_idx"
#: Indexes migration 0027 installed -- must survive the downgrade's
#: batch-mode table recreate.
_ORIGINAL_INDEXES: frozenset[str] = frozenset(
    {
        "event_outbox_tenant_unprocessed_idx",
        "event_outbox_unprocessed_idx",
    }
)


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0073.db"
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


def _table_columns(sync_url: str, table: str) -> set[str]:
    """Return the set of column names on *table* via ``PRAGMA``."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        eng.dispose()
    return {str(row[1]) for row in rows}


def _column_is_nullable(sync_url: str, table: str, column: str) -> bool:
    """Return True when *column* on *table* permits NULL."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on {table}")


def _table_indexes(sync_url: str, table: str) -> set[str]:
    """Return the set of index names declared on *table*."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        eng.dispose()
    return {str(row[1]) for row in rows}


def _index_sql(sync_url: str, name: str) -> str:
    """Return the stored ``CREATE INDEX`` DDL for index *name*."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"),
                {"n": name},
            ).first()
    finally:
        eng.dispose()
    if row is None or row[0] is None:
        raise AssertionError(f"index {name!r} has no stored DDL")
    return str(row[0])


def _seed_tenant(sync_url: str) -> str:
    """Insert one tenant (FK enforcement on); return its id string."""
    eng = sa_create_engine(sync_url)
    tenant_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "slug": f"t-{tenant_id[:8]}",
                    "name": "Dedupe Tenant",
                    "created_at": now,
                },
            )
    finally:
        eng.dispose()
    return tenant_id


def _insert_event(
    sync_url: str,
    tenant_id: str,
    *,
    dedupe_key: str | None,
    origin: str | None = None,
    event_kind: str = "ingest.test",
) -> None:
    """Insert one ``event_outbox`` row. Fresh connection so each row commits."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO event_outbox "
                    "(tenant_id, event_kind, payload, created_at, dedupe_key, origin) "
                    "VALUES (:tenant_id, :event_kind, '{}', :created_at, :dedupe_key, :origin)"
                ),
                {
                    "tenant_id": tenant_id,
                    "event_kind": event_kind,
                    "created_at": datetime.now(UTC).isoformat(),
                    "dedupe_key": dedupe_key,
                    "origin": origin,
                },
            )
    finally:
        eng.dispose()


def test_upgrade_adds_columns_nullable_and_partial_unique_index(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade 0073`` adds nullable ``dedupe_key`` / ``origin`` + the dedupe index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns = _table_columns(sync_url, "event_outbox")
    assert columns >= _NEW_COLUMNS, f"missing new columns: {_NEW_COLUMNS - columns}"
    for col in _NEW_COLUMNS:
        assert _column_is_nullable(sync_url, "event_outbox", col), f"{col} must be nullable"

    indexes = _table_indexes(sync_url, "event_outbox")
    assert _DEDUPE_INDEX in indexes, "upgrade must create the dedupe index"
    assert indexes >= _ORIGINAL_INDEXES, "upgrade must leave the 0027 indexes in place"

    # SQLite partial-index parity: the stored DDL must carry the UNIQUE
    # partial predicate, proving SQLite enforces the same shape PG does.
    ddl = _index_sql(sync_url, _DEDUPE_INDEX).upper()
    assert "UNIQUE" in ddl, f"dedupe index must be UNIQUE; got: {ddl}"
    assert "WHERE" in ddl and "DEDUPE_KEY IS NOT NULL" in ddl, (
        f"dedupe index must be partial on dedupe_key IS NOT NULL; got: {ddl}"
    )


def test_dedupe_key_unique_per_tenant_rejects_duplicate(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A duplicate ``(tenant_id, dedupe_key)`` raises :class:`IntegrityError`."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id = _seed_tenant(sync_url)

    _insert_event(sync_url, tenant_id, dedupe_key="dup-1", origin="src-A")
    with pytest.raises(IntegrityError):
        _insert_event(sync_url, tenant_id, dedupe_key="dup-1", origin="src-B")


def test_null_dedupe_key_unconstrained_and_uniqueness_is_per_tenant(
    alembic_cfg: tuple[Config, str],
) -> None:
    """NULL ``dedupe_key`` rows are unconstrained; the key is unique *per tenant*."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_a = _seed_tenant(sync_url)
    tenant_b = _seed_tenant(sync_url)

    # Internal producers leave dedupe_key NULL -- the partial predicate
    # keeps them out of the index, so many coexist in one tenant.
    for _ in range(3):
        _insert_event(sync_url, tenant_a, dedupe_key=None)

    # Same key under two tenants is fine (uniqueness is per tenant)...
    _insert_event(sync_url, tenant_a, dedupe_key="shared-key")
    _insert_event(sync_url, tenant_b, dedupe_key="shared-key")

    # ...but a second use within tenant_a still collides.
    with pytest.raises(IntegrityError):
        _insert_event(sync_url, tenant_a, dedupe_key="shared-key")


def test_downgrade_drops_columns_and_index_keeping_originals(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade 0072`` removes the columns + dedupe index; 0027 indexes survive."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    columns = _table_columns(sync_url, "event_outbox")
    assert not (_NEW_COLUMNS & columns), f"downgrade must drop {_NEW_COLUMNS & columns}"

    indexes = _table_indexes(sync_url, "event_outbox")
    assert _DEDUPE_INDEX not in indexes, "downgrade must drop the dedupe index"
    assert indexes >= _ORIGINAL_INDEXES, "the batch-mode recreate must preserve the 0027 indexes"


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade 0073`` → ``downgrade 0072`` → ``upgrade 0073`` restores the shape."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    tenant_id = _seed_tenant(sync_url)
    _insert_event(sync_url, tenant_id, dedupe_key="round-trip", origin="src")

    command.downgrade(cfg, _DOWN_REVISION)
    assert not (_NEW_COLUMNS & _table_columns(sync_url, "event_outbox"))

    command.upgrade(cfg, _REVISION)
    assert _table_columns(sync_url, "event_outbox") >= _NEW_COLUMNS
    assert _DEDUPE_INDEX in _table_indexes(sync_url, "event_outbox")

    # Enforcement works again post-round-trip (the dropped column's old
    # value is gone, so this fresh key inserts, then collides on reuse).
    _insert_event(sync_url, tenant_id, dedupe_key="post-rt")
    with pytest.raises(IntegrityError):
        _insert_event(sync_url, tenant_id, dedupe_key="post-rt")


def test_pre_upgrade_lacks_dedupe_columns(
    alembic_cfg: tuple[Config, str],
) -> None:
    """At revision 0072 the columns + dedupe index do not yet exist."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    columns = _table_columns(sync_url, "event_outbox")
    assert not (_NEW_COLUMNS & columns), "0072 must predate the dedupe columns"
    assert _DEDUPE_INDEX not in _table_indexes(sync_url, "event_outbox")
