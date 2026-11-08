# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The keyword half of operation search reads ``custom_description`` on PostgreSQL (#4753).

The unit tests (``tests/test_operations_custom_description_reaches_agent.py``)
run on SQLite, where the keyword half is an application-layer fallback.
This module checks what only PostgreSQL can show:

* ``search_operations`` finds an operation by words that are only in the
  admin's ``custom_description``, with a keyword score, and finds it by
  the original words again after the admin clears it.
* The keyword expression (``BM25_DOCUMENT_SQL``) is the one
  ``endpoint_descriptor_bm25_idx`` is built on, so PostgreSQL can use the
  index.
* Migration ``0145`` runs backward to ``0144`` (the index of ``0005``) and
  forward again.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_engine, get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._search import BM25_DOCUMENT_SQL
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.meta_tools import search_operations

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process engine at the testcontainer and seeds the two
# pinned tenant rows this suite scopes to.
from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CONNECTOR_ID = "acme-rest-2.0"
_OP = "GET:/api/widgets"
_INDEX = "endpoint_descriptor_bm25_idx"
_ORIGINAL = "Lists the gadget records of the account."
_CUSTOM = "Returns every widget in the inventory, with its serial number."


@pytest.fixture
def no_embedding(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


def _operator() -> Operator:
    return Operator(
        sub="op-custom-description",
        name="Custom Description Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed() -> None:
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=_TENANT,
                product="acme",
                version="2.0",
                impl_id="acme-rest",
                group_key="inventory",
                name="Inventory",
                when_to_use="Use it to read the inventory.",
                review_status="enabled",
            )
        )
        # The descriptors reference the group; write it first.
        await session.flush()
        session.add(
            EndpointDescriptor(
                tenant_id=_TENANT,
                product="acme",
                version="2.0",
                impl_id="acme-rest",
                op_id=_OP,
                source_kind="ingested",
                method="GET",
                path="/api/widgets",
                group_id=group_id,
                summary="List items.",
                description=_ORIGINAL,
                parameter_schema={"type": "object"},
                is_enabled=True,
            )
        )
        await session.commit()


async def _search(query: str) -> dict[str, Any]:
    return await search_operations(
        _operator(), {"connector_id": _CONNECTOR_ID, "query": query, "include_scores": True}
    )


@pytest.mark.asyncio
async def test_search_reads_custom_description_and_its_clear(
    pg_engine: None,  # noqa: F811 -- fixture
    no_embedding: None,
) -> None:
    await _seed()
    service = ReviewService(_operator())
    await service.edit_op(_CONNECTOR_ID, _OP, tenant_id=_TENANT, custom_description=_CUSTOM)

    found = await _search("widget serial")
    assert found["outcome"] == "matched", found
    assert [h["op_id"] for h in found["hits"]] == [_OP]
    assert found["hits"][0]["bm25_score"] is not None
    assert found["hits"][0]["description"] == _CUSTOM
    assert (await _search("gadget records"))["outcome"] == "no_match"

    await service.edit_op(_CONNECTOR_ID, _OP, tenant_id=_TENANT, custom_description=None)
    back = await _search("gadget records")
    assert back["outcome"] == "matched", back
    assert back["hits"][0]["description"] == _ORIGINAL
    assert (await _search("widget serial"))["outcome"] == "no_match"


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("Plans", []) for n in _plan_nodes(child))]


@pytest.mark.asyncio
async def test_keyword_expression_uses_the_index(
    pg_engine: None,  # noqa: F811 -- fixture
) -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        raw = (
            await conn.exec_driver_sql(
                "EXPLAIN (FORMAT JSON) SELECT id FROM endpoint_descriptor WHERE "
                + BM25_DOCUMENT_SQL
                + " @@ plainto_tsquery('english', 'widget')"
            )
        ).scalar_one()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    names = {node.get("Index Name") for node in _plan_nodes(plan[0]["Plan"])}
    assert _INDEX in names, json.dumps(plan, indent=1)


def _index_definition(url: str) -> str:
    async def _read() -> str:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                    {"name": _INDEX},
                )
                return str(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def test_migration_0145_runs_backward_and_forward(integration_env: None, async_pg_url: str) -> None:
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_pg_url)
    try:
        assert "custom_description" in _index_definition(async_pg_url)

        command.downgrade(cfg, "0144")
        old = _index_definition(async_pg_url)
        assert "custom_description" not in old
        assert "description" in old

        command.upgrade(cfg, "0145")
        assert "custom_description" in _index_definition(async_pg_url)
    finally:
        command.upgrade(cfg, "head")
        reset_engine_for_testing()
