# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How many rows the meaning half of the search returns on PostgreSQL (#6785).

The index ``documents_embedding_idx`` is IVFFlat with ``lists = 100``. Migration
``0003`` builds it on an empty table, as every new install does, so it has no
group centres. The tests load 300 rows with made-up text for one tenant, run
``ANALYZE`` (autovacuum does this on a live install), and count the rows that
``_cosine_candidates`` returns for a query asking for 50.

Result (pgvector on PostgreSQL 16): with ``RETRIEVAL_IVFFLAT_PROBES`` at 0 the
query returned **5 rows of 50**. The index looks into one group only. With the
setting at 100 it returned all 50.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.retrieval.retriever import (
    CANDIDATE_LIMIT,
    _cosine_candidates,
    _vector_literal,
)
from meho_backplane.settings import get_settings

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ROWS = 300
_DIM = 384


def _vector(rng: random.Random) -> list[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(_DIM)]


@pytest.fixture
async def loaded(pg_engine: None) -> AsyncIterator[list[float]]:  # noqa: F811
    """Load ``_ROWS`` made-up pages for one tenant; yield a query vector."""
    rng = random.Random(6785)
    async with get_sessionmaker()() as session:
        for i in range(_ROWS):
            await session.execute(
                text(
                    "INSERT INTO documents "
                    "(tenant_id, source, source_id, kind, body, body_hash, embedding) "
                    "VALUES (:t, 'kb', :sid, 'kb-entry', :body, :h, CAST(:emb AS vector))"
                ),
                {
                    "t": str(_TENANT),
                    "sid": f"page-{i}",
                    "body": f"Made-up page number {i} about gadgets.",
                    "h": f"hash-{i}",
                    "emb": _vector_literal(_vector(rng)),
                },
            )
        await session.commit()
    async with get_sessionmaker()() as session:
        # Autovacuum does this on a live install; the planner needs it to pick the index.
        await session.execute(text("ANALYZE documents"))
        await session.commit()
    yield _vector(rng)


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let a test set ``RETRIEVAL_IVFFLAT_PROBES``; the default is 0."""
    monkeypatch.delenv("RETRIEVAL_IVFFLAT_PROBES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_probes(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setenv("RETRIEVAL_IVFFLAT_PROBES", str(value))
    get_settings.cache_clear()


async def _count(query: list[float]) -> int:
    async with get_sessionmaker()() as session:
        rows = await _cosine_candidates(
            session, _TENANT, _vector_literal(query), None, None, None, None
        )
    return len(rows)


async def test_index_built_on_empty_table_returns_few_rows(
    loaded: list[float], probes: None
) -> None:
    """Proof: with the setting at 0 the query gets fewer rows than the 50 it asks for."""
    assert await _count(loaded) < CANDIDATE_LIMIT


async def test_all_groups_fill_the_candidate_list(
    loaded: list[float], probes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_probes(monkeypatch, 100)
    assert await _count(loaded) == min(CANDIDATE_LIMIT, _ROWS)


async def test_setting_does_not_leak_to_the_next_transaction(
    loaded: list[float], probes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_probes(monkeypatch, 100)
    async with get_sessionmaker()() as session:
        await _cosine_candidates(session, _TENANT, _vector_literal(loaded), None, None, None, None)
        inside = (await session.execute(text("SHOW ivfflat.probes"))).scalar_one()
        await session.commit()
        after = (await session.execute(text("SHOW ivfflat.probes"))).scalar_one()
    assert inside == "100"
    assert after == "1"
