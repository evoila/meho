# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A restored backplane database still holds a kb entry with its history.

The operator guide (``docs-site/install/upgrades.md``, "Back up and
restore the knowledge store") tells operators to take a ``pg_dump`` of
the backplane database and to restore it into a new, empty database.
This test follows those steps with the tools in the PostgreSQL image:

1. Write one entry with two revisions into a migrated pgvector database.
2. Take a dump with ``pg_dump --format=custom``.
3. Create a new database and restore the dump with ``pg_restore``.
4. Read the entry, its first revision and a search hit from the new
   database, and check that the passages kept their embeddings.

The embedding model is a deterministic stub, as in
:mod:`tests.integration.test_kb_service_pg`. The dump holds the stored
vectors, so the restore itself needs no model.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command
from sqlalchemy import text

from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.kb.schemas import KB_PASSAGE_SOURCE
from meho_backplane.kb.service import KbService
from tests._pgvector_image import pgvector_test_image

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, _async_url_from
from .test_kb_service_pg import _make_stub_embedding_service

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SLUG = "restore-runbook"
FIRST_BODY = "Restart the gateway with the blue wrench. Old term: quokkafirst."
SECOND_BODY = "Restart the gateway with the green wrench. New term: quokkasecond."
DUMP_FILE = "/tmp/meho-backplane.dump"
RESTORED_DB = "meho_restored"

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


@dataclass
class _Cluster:
    container: Any
    user: str
    source_db: str
    source_url: str
    restored_url: str

    def run(self, argv: list[str]) -> str:
        """Run a command inside the container; fail with its output."""
        result = self.container.exec(argv)
        output = result.output.decode("utf-8", errors="replace")
        assert result.exit_code == 0, f"{argv[0]} failed: {output}"
        return output


@pytest.fixture
def cluster() -> Iterator[_Cluster]:
    """A migrated pgvector database in a container, with its tools."""
    from testcontainers.postgres import PostgresContainer

    pg = PostgresContainer(pgvector_test_image())
    pg.start()
    try:
        source_url = _async_url_from(pg.get_connection_url())
        restored_url = source_url.rsplit("/", 1)[0] + f"/{RESTORED_DB}"
        previous_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = source_url
        try:
            cfg = alembic_config()
            cfg.set_main_option("sqlalchemy.url", source_url)
            command.upgrade(cfg, "head")
            yield _Cluster(pg, pg.username, pg.dbname, source_url, restored_url)
        finally:
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url
    finally:
        pg.stop()


@pytest.fixture
async def use_engine() -> AsyncIterator[Any]:
    """Point the backplane at one database; call again to switch."""
    reset_engine_for_testing()

    async def _use(url: str) -> None:
        current = engine_module._engine
        if current is not None:
            await current.dispose()
        reset_engine_for_testing()
        engine_module._engine = create_engine_for_url(url, pool_size=5, pool_timeout=10.0)

    yield _use
    current = engine_module._engine
    if current is not None:
        await current.dispose()
    reset_engine_for_testing()


async def _scalar(sql: str) -> Any:
    async with get_sessionmaker()() as session:
        return await session.scalar(text(sql))


@pytest.mark.asyncio
async def test_restored_database_keeps_entry_history_and_search(
    cluster: _Cluster, use_engine: Any
) -> None:
    fake = _make_stub_embedding_service()
    service = KbService()

    # 1. Write an entry with two revisions into the source database.
    await use_engine(cluster.source_url)
    async with get_sessionmaker()() as session:
        await session.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:id, 'tenant-a', 'Tenant A')"),
            {"id": str(TENANT_ID)},
        )
        await session.commit()
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        first_entry, _ = await service.create_entry(TENANT_ID, SLUG, FIRST_BODY)
        second_entry, _ = await service.create_entry(TENANT_ID, SLUG, SECOND_BODY)
    assert first_entry.id == second_entry.id
    assert first_entry.revision_id != second_entry.revision_id
    passages_before = await _scalar(
        f"SELECT count(*) FROM documents WHERE source = '{KB_PASSAGE_SOURCE}' "
        "AND embedding IS NOT NULL"
    )
    assert passages_before >= 1

    # 2. Dump the database. 3. Restore it into a new, empty database.
    cluster.run(
        ["pg_dump", "-U", cluster.user, "--format=custom", "-f", DUMP_FILE, cluster.source_db]
    )
    cluster.run(["createdb", "-U", cluster.user, RESTORED_DB])
    cluster.run(
        [
            "pg_restore",
            "-U",
            cluster.user,
            "-d",
            RESTORED_DB,
            "--no-owner",
            "--exit-on-error",
            DUMP_FILE,
        ]
    )

    # 4. Read from the restored database.
    await use_engine(cluster.restored_url)
    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        entry = await service.get_entry(TENANT_ID, SLUG)
        assert entry is not None
        assert entry.body == SECOND_BODY
        assert entry.id == second_entry.id
        assert entry.revision_id == second_entry.revision_id

        old = await service.get_revision(TENANT_ID, entry.id, first_entry.revision_id)
        assert old is not None
        assert old.body == FIRST_BODY
        assert old.revision_number == 1

        hits = await service.search_entries(TENANT_ID, "quokkasecond green wrench", limit=5)
        assert SLUG in [hit.slug for hit in hits]

    assert await _scalar("SELECT count(*) FROM kb_revision") == 2
    passages_after = await _scalar(
        f"SELECT count(*) FROM documents WHERE source = '{KB_PASSAGE_SOURCE}' "
        "AND embedding IS NOT NULL"
    )
    assert passages_after == passages_before
