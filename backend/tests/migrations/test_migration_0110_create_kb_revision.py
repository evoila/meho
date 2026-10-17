# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0110_create_kb_revision`` (#3753).

The migration adds the ``kb_revision`` table and copies one current
revision per existing kb entry. The tests check that ``upgrade``
creates the table, backfills only kb rows and labels them
``backfill-current``, that ``downgrade`` removes the table without
touching ``documents``, and that the pair round-trips. SQLite is the
test driver.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0110"
_DOWN_REVISION = "0109"
_TABLE = "kb_revision"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0110.db"
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


def _insert_document(
    sync_url: str,
    *,
    source: str,
    source_id: str,
    body: str,
    metadata: dict[str, object],
) -> uuid.UUID:
    doc_id = uuid.uuid4()
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, source, source_id, kind, body, "
                    "body_hash, tokens, embedding, metadata, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :source, :source_id, :kind, :body, :body_hash, "
                    "1, '[]', :metadata, '2026-01-01 00:00:00', '2026-01-02 00:00:00')"
                ),
                {
                    "id": doc_id.hex,
                    "tenant_id": uuid.uuid4().hex,
                    "source": source,
                    "source_id": source_id,
                    "kind": "kb-entry" if source == "kb" else "memory-user",
                    "body": body,
                    "body_hash": f"hash-{source_id}",
                    "metadata": json.dumps(metadata),
                },
            )
    finally:
        engine.dispose()
    return doc_id


def _tables(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names())
    finally:
        engine.dispose()


def _revisions(sync_url: str) -> list[dict[str, object]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"SELECT * FROM {_TABLE} ORDER BY body")).mappings()
            return [dict(row) for row in rows]
    finally:
        engine.dispose()


def test_upgrade_backfills_one_labelled_current_revision_per_kb_entry(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    provenance = {
        "source_kind": "directory",
        "relative_path": "ops/a.md",
        "ingested_at": "2026-01-01T00:00:00Z",
    }
    kb_a = _insert_document(
        sync_url,
        source="kb",
        source_id="a",
        body="body a",
        metadata={"provenance": provenance},
    )
    kb_b = _insert_document(sync_url, source="kb", source_id="b", body="body b", metadata={})
    _insert_document(sync_url, source="memory", source_id="m", body="memory", metadata={})

    command.upgrade(cfg, _REVISION)

    rows = _revisions(sync_url)
    assert [row["body"] for row in rows] == ["body a", "body b"]
    by_body = {row["body"]: row for row in rows}
    assert uuid.UUID(str(by_body["body a"]["entry_id"])) == kb_a
    assert uuid.UUID(str(by_body["body b"]["entry_id"])) == kb_b
    for row in rows:
        assert row["origin"] == "backfill-current"
        assert row["revision_number"] == 1
        assert row["embedding_model"] is None
        assert row["chunker_version"] is None
        assert row["created_at"] is not None
    assert by_body["body a"]["body_hash"] == "hash-a"
    assert json.loads(str(by_body["body a"]["provenance"])) == provenance
    assert by_body["body b"]["provenance"] is None


def test_downgrade_drops_table_and_keeps_documents(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    _insert_document(sync_url, source="kb", source_id="a", body="body a", metadata={})
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    assert _TABLE not in _tables(sync_url)
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM documents")).scalar() == 1
    finally:
        engine.dispose()


def test_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _tables(sync_url)
    indexes = set()
    engine = sa_create_engine(sync_url)
    try:
        with engine.connect() as conn:
            indexes = {str(index["name"]) for index in inspect(conn).get_indexes(_TABLE)}
    finally:
        engine.dispose()
    assert "kb_revision_entry_number_idx" in indexes
