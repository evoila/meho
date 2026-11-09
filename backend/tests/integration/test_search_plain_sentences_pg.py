# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Plain sentences in ``search_operations`` on PostgreSQL (#5490).

The unit tests (``tests/test_search_plain_sentences.py``) run on SQLite,
where the keyword half is an application-layer fallback. This module runs
the real retriever (PostgreSQL full text search plus pgvector) and checks:

* A sentence of many words gets no keyword score when one word is missing
  from the operation's text. With the ``SEARCH_PLAIN_SENTENCES`` switch off
  (the default) the search answers ``ambiguous``; with it on, the top hit
  holds words of the sentence, so it answers ``matched``.
* The switch never changes the hit order or the scores.
* A top hit that holds no word of the query stays ``ambiguous``, also with
  the switch on. So does a query of stop words only.
* The check reads the description the agent reads: the admin's
  ``custom_description`` only while ``AGENT_READS_CUSTOM_DESCRIPTION`` is on.
* A ``group`` filter that leaves one operation answers ``matched`` with the
  switch on, also for words that match nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CONNECTOR_ID = "acme-rest-2.0"
#: Eleven words. "fetch" is in no operation's text, so the every-word
#: keyword match finds nothing.
_SENTENCE = "fetch arbitrary properties of one managed object by type and moid"
#: (op_id, group_key, summary, description, distance in meaning). The right
#: operation for ``_SENTENCE`` is the closest in meaning.
_OPS = (
    (
        "acme.object.collect",
        "object-collect",
        "Read properties of a managed object.",
        "Reads arbitrary properties of one managed object, by its type and moid.",
        0.1,
    ),
    ("acme.object.tag", "tags", "Tag an object.", "Adds a label to it.", 0.3),
    ("acme.snapshot.delete", "tags", "Delete a snapshot.", "Removes it from the disk chain.", 0.6),
)


def _vector(distance: float) -> list[float]:
    return [1.0, distance] + [0.0] * (EMBEDDING_DIMENSION - 2)


def _probe_every_list(dbapi_connection: Any, *_: Any) -> None:
    # The vector index (IVFFlat, 100 lists) was built on an empty table, so
    # probing one list can miss the three test rows. Probing every list
    # makes the meaning search exact.
    cursor = dbapi_connection.cursor()
    cursor.execute("SET ivfflat.probes = 100")
    cursor.close()


@pytest.fixture
def fixed_vectors(
    pg_engine: None,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """A fixed query vector; the rows' cosine order comes from ``_OPS``."""
    service = AsyncMock()
    service.encode_one.return_value = _vector(0.0)
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    engine = get_engine().sync_engine
    event.listen(engine, "checkout", _probe_every_list)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    event.remove(engine, "checkout", _probe_every_list)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _set_switch(monkeypatch: pytest.MonkeyPatch, name: str, *, on: bool) -> None:
    monkeypatch.setenv(name, "true" if on else "false")
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-plain-sentences",
        name="Plain Sentences Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed() -> None:
    """Seed the groups and operations; remove this connector's rows first."""
    async with get_sessionmaker()() as session:
        for table in (EndpointDescriptor, OperationGroup):
            await session.execute(
                delete(table).where(table.tenant_id == _TENANT, table.product == "acme")
            )
        group_ids: dict[str, uuid.UUID] = {}
        for group_key in sorted({op[1] for op in _OPS}):
            group_ids[group_key] = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_ids[group_key],
                    tenant_id=_TENANT,
                    product="acme",
                    version="2.0",
                    impl_id="acme-rest",
                    group_key=group_key,
                    name=group_key,
                    when_to_use=f"Use it for {group_key}.",
                    review_status="enabled",
                )
            )
        await session.flush()
        for op_id, group_key, summary, description, distance in _OPS:
            session.add(
                EndpointDescriptor(
                    tenant_id=_TENANT,
                    product="acme",
                    version="2.0",
                    impl_id="acme-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/{op_id}",
                    group_id=group_ids[group_key],
                    summary=summary,
                    description=description,
                    parameter_schema={"type": "object"},
                    embedding=_vector(distance),
                    is_enabled=True,
                )
            )
        await session.commit()


async def _search(query: str, group: str | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "connector_id": _CONNECTOR_ID,
        "query": query,
        "include_scores": True,
    }
    if group is not None:
        arguments["group"] = group
    return await search_operations(_operator(), arguments)


@pytest.mark.asyncio
async def test_long_sentence_is_matched_only_with_the_switch(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    assert len(_SENTENCE.split()) >= 8

    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=False)
    off = await _search(_SENTENCE)
    assert off["outcome"] == "ambiguous", off
    assert off["hits"][0]["op_id"] == "acme.object.collect"
    assert all(hit["bm25_score"] is None for hit in off["hits"])

    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=True)
    on = await _search(_SENTENCE)
    assert on["outcome"] == "matched", on
    assert on["advisory"] is None
    # Same hits, same order, same scores: only the outcome changes.
    assert on["hits"] == off["hits"]


@pytest.mark.asyncio
async def test_top_hit_without_a_query_word_stays_ambiguous(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a lower hit holds words of the query; the outcome reads the top hit."""
    await _seed()
    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=True)
    result = await _search("wipe the snapshot chain please")
    assert result["hits"][0]["op_id"] == "acme.object.collect"
    assert result["outcome"] == "ambiguous", result


@pytest.mark.asyncio
async def test_stop_words_only_stay_ambiguous(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=True)
    result = await _search("the of and by")
    assert result["outcome"] == "ambiguous", result
    assert all(hit["bm25_score"] is None for hit in result["hits"])


@pytest.mark.parametrize(
    ("reads_custom", "expected"),
    [
        pytest.param(False, "ambiguous", id="custom-switch-off"),
        pytest.param(True, "matched", id="custom-switch-on"),
    ],
)
@pytest.mark.asyncio
async def test_check_reads_the_description_the_agent_reads(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
    reads_custom: bool,
    expected: str,
) -> None:
    """The word "inventory" is only in the admin's text of the top hit."""
    await _seed()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.tenant_id == _TENANT,
                EndpointDescriptor.op_id == "acme.object.collect",
            )
            .values(custom_description="Shows the inventory record of one item.")
        )
        await session.commit()
    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=True)
    _set_switch(monkeypatch, "AGENT_READS_CUSTOM_DESCRIPTION", on=reads_custom)

    result = await _search("zebra inventory quartz")
    assert result["hits"][0]["op_id"] == "acme.object.collect"
    assert result["outcome"] == expected, result


@pytest.mark.asyncio
async def test_one_operation_group(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=False)
    off = await _search("zebra quartz", group="object-collect")
    assert off["outcome"] == "ambiguous", off

    _set_switch(monkeypatch, "SEARCH_PLAIN_SENTENCES", on=True)
    on = await _search("zebra quartz", group="object-collect")
    two = await _search("zebra quartz", group="tags")
    assert on["outcome"] == "matched", on
    assert [hit["op_id"] for hit in on["hits"]] == ["acme.object.collect"]
    assert two["outcome"] == "ambiguous", two
