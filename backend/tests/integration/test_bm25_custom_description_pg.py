# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The keyword half of operation search reads ``custom_description`` on PostgreSQL (#4753).

The unit tests (``tests/test_operations_custom_description_reaches_agent.py``)
run on SQLite, where the keyword half is an application-layer fallback.
This module checks what only PostgreSQL can show:

* With the ``AGENT_READS_CUSTOM_DESCRIPTION`` switch on, ``search_operations``
  finds an operation by words that are only in the admin's
  ``custom_description``, with a keyword score, and finds it by the
  original words again after the admin clears it.
* With the switch off (the default), a saved override changes nothing: the
  original words find the operation and the hit shows the original text.
* Each state's keyword expression is the one its index is built on:
  ``BM25_ORIGINAL_DOCUMENT_SQL`` and ``endpoint_descriptor_bm25_idx``
  (migration ``0005``), ``BM25_CUSTOM_DOCUMENT_SQL`` and
  ``endpoint_descriptor_bm25_custom_idx`` (migration ``0147``).
* Migration ``0147`` runs backward to ``0146`` and forward again, and
  leaves the index of ``0005`` alone.
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
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_engine, get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._search import (
    BM25_CUSTOM_DOCUMENT_SQL,
    BM25_ORIGINAL_DOCUMENT_SQL,
)
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process engine at the testcontainer and seeds the two
# pinned tenant rows this suite scopes to.
from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CONNECTOR_ID = "acme-rest-2.0"
_OP = "GET:/api/widgets"
_ORIGINAL_INDEX = "endpoint_descriptor_bm25_idx"
_CUSTOM_INDEX = "endpoint_descriptor_bm25_custom_idx"
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


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn ``AGENT_READS_CUSTOM_DESCRIPTION`` on; ``integration_env`` pins the rest."""
    monkeypatch.setenv("AGENT_READS_CUSTOM_DESCRIPTION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
    """Seed one group and one operation; remove this connector's rows first.

    ``pg_engine`` truncates the chassis tables between tests, but not
    ``endpoint_descriptor`` or ``operation_group``, so each test here starts
    by removing what the previous one wrote.
    """
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        for table in (EndpointDescriptor, OperationGroup):
            await session.execute(
                delete(table).where(table.tenant_id == _TENANT, table.product == "acme")
            )
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
    switch_on: None,
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


@pytest.mark.asyncio
async def test_switch_off_search_reads_the_original_text(
    pg_engine: None,  # noqa: F811 -- fixture
    no_embedding: None,
) -> None:
    """A saved override changes nothing while the switch is off (the default)."""
    await _seed()
    service = ReviewService(_operator())
    await service.edit_op(_CONNECTOR_ID, _OP, tenant_id=_TENANT, custom_description=_CUSTOM)

    found = await _search("gadget records")
    assert found["outcome"] == "matched", found
    assert found["hits"][0]["bm25_score"] is not None
    assert found["hits"][0]["description"] == _ORIGINAL
    assert (await _search("widget serial"))["outcome"] == "no_match"


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("Plans", []) for n in _plan_nodes(child))]


@pytest.mark.parametrize(
    ("document_sql", "index_name"),
    [
        pytest.param(BM25_ORIGINAL_DOCUMENT_SQL, _ORIGINAL_INDEX, id="switch-off"),
        pytest.param(BM25_CUSTOM_DOCUMENT_SQL, _CUSTOM_INDEX, id="switch-on"),
    ],
)
@pytest.mark.asyncio
async def test_each_keyword_expression_uses_its_index(
    pg_engine: None,  # noqa: F811 -- fixture
    document_sql: str,
    index_name: str,
) -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        raw = (
            await conn.exec_driver_sql(
                "EXPLAIN (FORMAT JSON) SELECT id FROM endpoint_descriptor WHERE "
                + document_sql
                + " @@ plainto_tsquery('english', 'widget')"
            )
        ).scalar_one()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    names = {node.get("Index Name") for node in _plan_nodes(plan[0]["Plan"])}
    assert index_name in names, json.dumps(plan, indent=1)


def _index_definitions(url: str) -> dict[str, str]:
    """Return ``{index name: definition}`` for the two keyword indexes that exist."""

    async def _read() -> dict[str, str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE indexname IN (:original, :custom)"
                    ),
                    {"original": _ORIGINAL_INDEX, "custom": _CUSTOM_INDEX},
                )
                return {str(name): str(definition) for name, definition in result.all()}
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def test_migration_0147_runs_backward_and_forward(integration_env: None, async_pg_url: str) -> None:
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_pg_url)
    try:
        at_head = _index_definitions(async_pg_url)
        assert "custom_description" in at_head[_CUSTOM_INDEX]
        assert "custom_description" not in at_head[_ORIGINAL_INDEX]

        command.downgrade(cfg, "0146")
        before = _index_definitions(async_pg_url)
        assert _CUSTOM_INDEX not in before
        assert before[_ORIGINAL_INDEX] == at_head[_ORIGINAL_INDEX]

        command.upgrade(cfg, "0147")
        assert _index_definitions(async_pg_url) == at_head
    finally:
        command.upgrade(cfg, "head")
        reset_engine_for_testing()
