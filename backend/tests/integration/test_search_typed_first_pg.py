# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``SEARCH_TYPED_FIRST`` switch against the real retriever (#4712).

PostgreSQL full text search plus pgvector. Many raw operations match the
query better than the one multi-step operation, so without the switch the
multi-step operation is not first. With the switch on, the candidate
queries leave raw rows out, and it is first.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111114712")
_CONNECTOR_ID = "vmware-rest-8.0"
_QUERY = "delete content library item"
_COMPOSITE = "vmware.composite.content_library.item.delete"
#: (op_id, source_kind, summary, distance in meaning). Smaller is closer.
_OPS: tuple[tuple[str, str, str, float], ...] = (
    *(
        (
            f"DELETE:/api/content/library/item/{n}",
            "ingested",
            "Delete content library item. Delete a content library item now.",
            0.05 + n * 0.01,
        )
        for n in range(12)
    ),
    (_COMPOSITE, "composite", "Delete a content library item safely.", 0.5),
    ("vmware.content_library.item.list", "typed", "List content library items.", 0.6),
)


def _vector(distance: float) -> list[float]:
    return [1.0, distance] + [0.0] * (EMBEDDING_DIMENSION - 2)


def _probe_every_list(dbapi_connection: Any, *_: Any) -> None:
    # The vector index was built on an empty table; probing every list
    # makes the meaning search exact for these few rows.
    cursor = dbapi_connection.cursor()
    cursor.execute("SET ivfflat.probes = 100")
    cursor.close()


@pytest.fixture
def fixed_vectors(
    pg_engine: None,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
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


def _set_switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("SEARCH_TYPED_FIRST", "true" if on else "false")
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-typed-first",
        name="Typed First Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed(rows: tuple[tuple[str, str, str, float], ...]) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == _TENANT,
                EndpointDescriptor.product == "vmware",
            )
        )
        for op_id, kind, summary, distance in rows:
            raw = kind == "ingested"
            session.add(
                EndpointDescriptor(
                    tenant_id=_TENANT,
                    product="vmware",
                    version="8.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind=kind,
                    method="DELETE" if raw else None,
                    path=f"/api/{uuid.uuid4()}" if raw else None,
                    summary=summary,
                    description=summary,
                    parameter_schema={"type": "object"},
                    embedding=_vector(distance),
                    is_enabled=True,
                )
            )
        await session.commit()


async def _search(query: str, **extra: Any) -> dict[str, Any]:
    return await search_operations(
        _operator(), {"connector_id": _CONNECTOR_ID, "query": query, **extra}
    )


def _ids(answer: dict[str, Any]) -> list[str]:
    return [hit["op_id"] for hit in answer["hits"]]


@pytest.mark.asyncio
async def test_multi_step_operation_is_first_only_with_the_switch(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(_OPS)

    _set_switch(monkeypatch, on=False)
    off = await _search(_QUERY)
    assert _ids(off)[0] != _COMPOSITE, off
    assert "raw_operations" not in off

    _set_switch(monkeypatch, on=True)
    on = await _search(_QUERY)
    assert _ids(on)[0] == _COMPOSITE, on
    assert all(not op_id.startswith("DELETE:") for op_id in _ids(on)), on
    assert on["raw_operations"]["included"] is False

    shown = await _search(_QUERY, include_raw=True)
    assert any(op_id.startswith("DELETE:") for op_id in _ids(shown)), shown
    assert shown["raw_operations"]["included"] is True


@pytest.mark.asyncio
async def test_only_raw_matches_say_how_many_were_left_out(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(
        (
            ("POST:/api/tagging/attach", "ingested", "Attach a tag to an object.", 0.1),
            ("vmware.vm.list", "typed", "List virtual machines.", 0.9),
        )
    )
    _set_switch(monkeypatch, on=True)
    answer = await _search("attach tag", limit=1)
    assert "POST:/api/tagging/attach" not in _ids(answer)
    assert answer["outcome"] in ("no_match", "ambiguous"), answer
    assert answer["raw_operations"]["hidden_matches"] == 1, answer
    assert "include_raw" in answer["raw_operations"]["note"]
