# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search filters for knowledge and memory against a real PostgreSQL.

Proves the SQL side of the filters: product, version, status and tags
(metadata containment), the ``confirmed_at`` and last-write time limits
(the time window predicate), and the shared knowledge scope seen from a
second tenant. The wiring is covered without PostgreSQL in
``tests/test_search_filters.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.kb.service import KbService
from meho_backplane.memory.service import MemoryService
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.time_window import TimeWindow
from meho_backplane.settings import get_settings

from .conftest import DOCKER_AVAILABLE, SKIP_REASON
from .test_kb_service_pg import _make_stub_embedding_service

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

OWNER = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")
QUERY = "snapshot clone runbook"
AUG_1 = datetime(2026, 8, 1, tzinfo=UTC)
OCT_1 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def stub_embedding(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(OWNER))
    get_settings.cache_clear()
    fake = _make_stub_embedding_service()
    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        yield
    get_settings.cache_clear()


def _header(**fields: Any) -> dict[str, Any]:
    return {"type": "lesson", "product": "vcenter", **fields}


async def _seed_knowledge() -> None:
    service = KbService()
    notes: dict[str, dict[str, Any]] = {
        "vc90-lesson": _header(
            version="9.0", status="confirmed", tags=["vm", "snapshot"], confirmed_at="2026-10-15"
        ),
        "vc80-lesson": _header(
            version="8.0", status="confirmed", tags=["vm"], confirmed_at="2026-05-01"
        ),
        "vc90-candidate": _header(version="9.0", status="candidate", tags=["vm"]),
        "company-note": _header(
            version="9.0", status="confirmed", confirmed_at="2026-10-20", shared=True
        ),
    }
    for slug, metadata in notes.items():
        await service.create_entry(OWNER, slug, f"{QUERY} {slug}", metadata)
    await service.create_entry(OTHER, "other-note", f"{QUERY} other-note", _header(version="9.0"))
    # Rows written before the header check: dates that are odd or have
    # no offset. A bad one must not abort the query.
    for slug, confirmed in (
        ("odd-date", "soon"),
        ("no-offset", "2026-10-02T00:00:00"),
        ("east-offset", "2026-10-01T01:00:00+02:00"),
    ):
        await index_document(
            tenant_id=OWNER,
            source="kb",
            source_id=slug,
            kind="kb-entry",
            body=f"{QUERY} {slug}",
            metadata={"confirmed_at": confirmed},
        )


async def _set_updated_at(tenant_id: uuid.UUID, slug: str, when: datetime) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "UPDATE documents SET updated_at = :when WHERE tenant_id = :tenant"
                " AND (source_id = :slug OR metadata -> 'kb_passage' ->> 'slug' = :slug)"
            ),
            {"when": when, "tenant": tenant_id, "slug": slug},
        )
        await session.commit()


async def _slugs(tenant_id: uuid.UUID, **kwargs: Any) -> set[str]:
    hits = await KbService().search_entries(tenant_id, QUERY, limit=50, **kwargs)
    return {hit.slug for hit in hits}


async def test_knowledge_filters_on_product_version_status_and_tags(
    pg_engine: None, stub_embedding: None
) -> None:
    await _seed_knowledge()
    assert await _slugs(OWNER, filters={"product": "vcenter", "version": "9.0"}) == {
        "vc90-lesson",
        "vc90-candidate",
        "company-note",
    }
    assert await _slugs(OWNER, filters={"version": "9.0", "status": "confirmed"}) == {
        "vc90-lesson",
        "company-note",
    }
    assert await _slugs(OWNER, filters={"tags": ["vm", "snapshot"]}) == {"vc90-lesson"}
    assert await _slugs(OWNER, filters={"tags": ["vm"]}) == {
        "vc90-lesson",
        "vc80-lesson",
        "vc90-candidate",
    }
    assert await _slugs(OWNER, filters={"tags": ["unknown"]}) == set()


async def test_knowledge_filters_on_confirmed_at(pg_engine: None, stub_embedding: None) -> None:
    await _seed_knowledge()
    after_oct = TimeWindow(confirmed_after=OCT_1)
    # The odd date is left out without an error; a time without an
    # offset is UTC; 01:00 at +02:00 is still 30 September in UTC.
    assert await _slugs(OWNER, time_window=after_oct) == {
        "vc90-lesson",
        "company-note",
        "no-offset",
    }
    before_aug = TimeWindow(confirmed_before=AUG_1)
    assert await _slugs(OWNER, time_window=before_aug) == {"vc80-lesson"}
    both = TimeWindow(confirmed_after=OCT_1, confirmed_before=datetime(2026, 10, 15, tzinfo=UTC))
    assert await _slugs(OWNER, time_window=both) == {"vc90-lesson", "no-offset"}


async def test_knowledge_filters_on_the_last_write(pg_engine: None, stub_embedding: None) -> None:
    await _seed_knowledge()
    await _set_updated_at(OWNER, "vc80-lesson", datetime(2026, 1, 10, tzinfo=UTC))
    old = TimeWindow(updated_before=datetime(2026, 2, 1, tzinfo=UTC))
    assert await _slugs(OWNER, time_window=old) == {"vc80-lesson"}
    recent = TimeWindow(updated_after=datetime(2026, 2, 1, tzinfo=UTC))
    assert "vc80-lesson" not in await _slugs(OWNER, time_window=recent)


async def test_a_second_tenant_finds_the_shared_note_with_its_source_tenant(
    pg_engine: None, stub_embedding: None
) -> None:
    await _seed_knowledge()
    hits = await KbService().search_entries(OTHER, QUERY, limit=50)
    by_slug = {hit.slug: hit for hit in hits}
    assert set(by_slug) == {"company-note", "other-note"}
    assert (by_slug["company-note"].tenant_id, by_slug["company-note"].shared) == (OWNER, True)
    assert (by_slug["other-note"].tenant_id, by_slug["other-note"].shared) == (OTHER, False)

    assert await _slugs(OTHER, scope="tenant") == {"other-note"}
    assert await _slugs(OTHER, scope="shared") == {"company-note"}
    assert await _slugs(OWNER, scope="shared") == {"company-note"}
    # The filters apply to the shared scope too.
    assert await _slugs(OTHER, filters={"status": "confirmed"}) == {"company-note"}
    assert await _slugs(OTHER, time_window=TimeWindow(confirmed_before=AUG_1)) == set()


async def _seed_memory() -> None:
    rows: dict[str, dict[str, Any]] = {
        "mem-confirmed": {
            "status": "confirmed",
            "product": "vcenter",
            "version": "9.0",
            "tags": ["vm", "snapshot"],
            "confirmed_at": "2026-10-15T00:00:00+00:00",
        },
        "mem-candidate": {"status": "candidate", "product": "vcenter", "tags": ["vm"]},
        "mem-old": {
            "status": "confirmed",
            "version": "8.0",
            "confirmed_at": "2026-03-01T00:00:00Z",
        },
    }
    for slug, metadata in rows.items():
        await index_document(
            tenant_id=OWNER,
            source="memory",
            source_id=f"tenant:-:-:{slug}",
            kind="memory-tenant",
            body=f"{QUERY} {slug}",
            metadata={"scope": "tenant", "user_sub": None, "target_name": None, **metadata},
        )


async def test_memory_filters(pg_engine: None, stub_embedding: None) -> None:
    await _seed_memory()
    operator = Operator(
        sub="op-1",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OWNER,
        tenant_role=TenantRole.OPERATOR,
    )
    service = MemoryService()

    async def slugs(**kwargs: Any) -> set[str]:
        return {hit.slug for hit in await service.search_memories(operator, QUERY, **kwargs)}

    assert await slugs() == {"mem-confirmed", "mem-candidate", "mem-old"}
    assert await slugs(metadata_filters={"status": "confirmed"}) == {"mem-confirmed", "mem-old"}
    assert await slugs(metadata_filters={"product": "vcenter", "version": "9.0"}) == {
        "mem-confirmed"
    }
    assert await slugs(metadata_filters={"tags": ["vm"]}) == {"mem-confirmed", "mem-candidate"}
    assert await slugs(time_window=TimeWindow(confirmed_after=AUG_1)) == {"mem-confirmed"}
    assert await slugs(time_window=TimeWindow(confirmed_before=AUG_1)) == {"mem-old"}
    future = TimeWindow(updated_after=datetime(2100, 1, 1, tzinfo=UTC))
    assert await slugs(time_window=future) == set()
