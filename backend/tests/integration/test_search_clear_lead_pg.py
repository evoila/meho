# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``SEARCH_CLEAR_LEAD`` switch against the real retriever (#6014).

PostgreSQL full text search plus pgvector, with the operations of a live
search for "list virtual machines" on a VMware connector. The right
operation gets no keyword score, because its summary says "VMs". The
keyword hits above it are operations about one VM.
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

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111116014")
_CONNECTOR_ID = "vmware-rest-8.0"
_QUERY = "list virtual machines"
_RIGHT_OP = "vmware.vm.list"
#: (op_id, summary, description, distance in meaning). Smaller is closer.
_OPS = (
    (
        "GET:/vcenter/vm/{vm}/data-sets",
        "List data sets of one virtual machine.",
        "Lists data sets of a virtual machine. Machines keep data sets per VM.",
        0.90,
    ),
    (
        "GET:/vcenter/storage/policies/compliance/vm",
        "List compliance of virtual machines.",
        "Lists compliance for virtual machines. List filtered by policy.",
        0.80,
    ),
    (
        "GET:/vcenter/vm/{vm}/hardware/boot/device",
        "List boot devices of a virtual machine.",
        "Lists boot devices. List virtual machines hardware boot order. "
        "List machines. Virtual list.",
        0.70,
    ),
    ("GET:/vcenter/vm/{vm}/hardware/disk", "Disks of one guest.", "Disks attached.", 0.10),
    (_RIGHT_OP, "VMs in the inventory.", "All VMs with name and power state.", 0.20),
)
_CLEAR_QUERY = "snapshot delete"
_CLEAR_OP = "vmware.snapshot.delete"


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
    monkeypatch.setenv("SEARCH_CLEAR_LEAD", "true" if on else "false")
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-clear-lead",
        name="Clear Lead Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed(rows: tuple[tuple[str, str, str, float], ...]) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == _TENANT,
                EndpointDescriptor.product == "vmware",
            )
        )
        for op_id, summary, description, distance in rows:
            session.add(
                EndpointDescriptor(
                    tenant_id=_TENANT,
                    product="vmware",
                    version="8.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/{uuid.uuid4()}",
                    summary=summary,
                    description=description,
                    parameter_schema={"type": "object"},
                    embedding=_vector(distance),
                    is_enabled=True,
                )
            )
        await session.commit()


async def _search(query: str, **extra: Any) -> dict[str, Any]:
    return await search_operations(
        _operator(),
        {"connector_id": _CONNECTOR_ID, "query": query, "include_scores": True, **extra},
    )


@pytest.mark.asyncio
async def test_right_operation_not_first_is_ambiguous_only_with_the_switch(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(_OPS)

    _set_switch(monkeypatch, on=False)
    off = await _search(_QUERY)
    assert off["hits"][0]["op_id"] != _RIGHT_OP, off
    assert off["hits"][0]["bm25_score"] is not None
    assert next(h for h in off["hits"] if h["op_id"] == _RIGHT_OP)["bm25_score"] is None
    assert off["outcome"] == "matched", off

    _set_switch(monkeypatch, on=True)
    on = await _search(_QUERY)
    assert on["outcome"] == "ambiguous", on
    # Only the outcome changes: same hits, same order, same scores.
    assert on["hits"] == off["hits"]


@pytest.mark.asyncio
async def test_limit_one_still_answers_ambiguous_with_the_switch(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(_OPS)
    _set_switch(monkeypatch, on=True)
    one = await _search(_QUERY, limit=1)
    assert one["outcome"] == "ambiguous", one
    assert len(one["hits"]) == 1


@pytest.mark.asyncio
async def test_one_clear_top_hit_stays_matched_with_the_switch(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(
        (
            (_CLEAR_OP, "Delete a snapshot.", "Delete one snapshot from the chain.", 0.05),
            ("vmware.snapshot.list", "List snapshots.", "Lists every snapshot.", 0.5),
            ("vmware.vm.get", "Read one VM.", "Reads a VM.", 0.9),
        )
    )
    _set_switch(monkeypatch, on=True)
    result = await _search(_CLEAR_QUERY)
    assert result["outcome"] == "matched", result
    assert result["hits"][0]["op_id"] == _CLEAR_OP


@pytest.mark.asyncio
async def test_closer_hit_below_tenth_answers_ambiguous_with_the_switch(
    pg_engine: None,  # noqa: F811 -- fixture
    fixed_vectors: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten keyword hits above the right operation, which ranks eleventh."""
    keyword_ops = tuple(
        (
            f"GET:/vcenter/vm/{{vm}}/part-{n}",
            "List virtual machines parts." if n == 0 else f"List part {n} of virtual machines.",
            "List virtual machines. " * (10 - n),
            0.30 + n * 0.05,
        )
        for n in range(10)
    )
    await _seed((*keyword_ops, (_RIGHT_OP, "VMs in the inventory.", "All VMs.", 0.10)))

    _set_switch(monkeypatch, on=False)
    off = await _search(_QUERY, limit=20)
    assert [h["op_id"] for h in off["hits"]].index(_RIGHT_OP) == 10, off
    default_off = await _search(_QUERY)
    assert _RIGHT_OP not in [h["op_id"] for h in default_off["hits"]]
    assert default_off["outcome"] == "matched", default_off

    _set_switch(monkeypatch, on=True)
    default_on = await _search(_QUERY)
    assert default_on["outcome"] == "ambiguous", default_on
    # Only the outcome changes: same hits, same order, same scores.
    assert default_on["hits"] == default_off["hits"]
