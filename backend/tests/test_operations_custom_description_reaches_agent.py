# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An admin's ``custom_description`` is the text the agent reads (#4753).

An admin can fix a weak or misleading operation description with
``meho_connector_edit_op`` (``custom_description``). These tests check that
the agent then sees the admin's text, not the original one:

* in each ``search_operations`` hit and in the exact-op contract;
* in the keyword half of search, so a query that matches only the admin's
  words finds the operation with a keyword score and the ``matched`` label;
* after the admin clears it (``null``), the original text is back, in the
  hit, the contract and the keyword half.

The edit goes through :meth:`ReviewService.edit_op`, the same service the
MCP tool and the REST route use. Unit tests run on SQLite, so the keyword
half is the application-layer fallback; the PostgreSQL query and its index
are checked in ``tests/integration/test_bm25_custom_description_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0de")
_CONNECTOR_ID = "acme-rest-2.0"
_OP = "GET:/api/widgets"
_OTHER_OP = "GET:/api/gadgets"
_ORIGINAL = "Lists the gadget records of the account."
_CUSTOM = "Returns every widget in the inventory, with its serial number."


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The query embedding has no rows to compare with; only keywords rank."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)


def _operator(role: TenantRole = TenantRole.TENANT_ADMIN) -> Operator:
    return Operator(
        sub="op-custom-description",
        name="Custom Description Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=role,
    )


async def _seed() -> None:
    """One enabled group with two enabled ingested reads; no embeddings."""
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
        for op_id, summary in ((_OP, "List items."), (_OTHER_OP, "Read the account.")):
            method, _, path = op_id.partition(":")
            session.add(
                EndpointDescriptor(
                    tenant_id=_TENANT,
                    product="acme",
                    version="2.0",
                    impl_id="acme-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    group_id=group_id,
                    summary=summary,
                    description=_ORIGINAL if op_id == _OP else "Reads the account settings.",
                    parameter_schema={"type": "object"},
                    is_enabled=True,
                )
            )
        await session.commit()


async def _edit(custom_description: str | None) -> None:
    await ReviewService(_operator()).edit_op(
        _CONNECTOR_ID, _OP, tenant_id=_TENANT, custom_description=custom_description
    )


async def _search(query: str) -> dict[str, Any]:
    return await search_operations(
        _operator(), {"connector_id": _CONNECTOR_ID, "query": query, "include_scores": True}
    )


async def _contract() -> dict[str, Any]:
    result = await search_operations(
        _operator(TenantRole.OPERATOR), {"connector_id": _CONNECTOR_ID, "op_id": _OP}
    )
    contract: dict[str, Any] = result["operation"]
    return contract


def _hit(result: dict[str, Any], op_id: str = _OP) -> dict[str, Any]:
    hits = [h for h in result["hits"] if h["op_id"] == op_id]
    assert len(hits) == 1, result
    return hits[0]


@pytest.mark.asyncio
async def test_custom_description_is_the_description_in_hit_and_contract() -> None:
    await _seed()
    await _edit(_CUSTOM)

    hit = _hit(await _search("widget serial"))
    assert hit["description"] == _CUSTOM
    contract = await _contract()
    assert contract["description"] == _CUSTOM
    # The other operation has no override and keeps its own text.
    assert _hit(await _search("account settings"), _OTHER_OP)["description"] == (
        "Reads the account settings."
    )


@pytest.mark.asyncio
async def test_query_matching_only_custom_words_is_a_keyword_match() -> None:
    await _seed()
    await _edit(_CUSTOM)

    result = await _search("widget serial")
    assert [h["op_id"] for h in result["hits"]] == [_OP]
    assert result["hits"][0]["bm25_score"] is not None
    assert result["outcome"] == "matched"
    # The replaced text no longer matches on words.
    assert (await _search("gadget records"))["outcome"] == "no_match"


@pytest.mark.asyncio
async def test_clearing_custom_description_brings_back_the_original() -> None:
    await _seed()
    await _edit(_CUSTOM)
    await _edit(None)

    result = await _search("gadget records")
    assert result["outcome"] == "matched"
    assert _hit(result)["description"] == _ORIGINAL
    assert (await _contract())["description"] == _ORIGINAL
    assert (await _search("widget serial"))["outcome"] == "no_match"


@pytest.mark.asyncio
async def test_empty_custom_description_falls_back_to_the_original() -> None:
    """An empty string is no override, the same as the SQL ``nullif`` rule."""
    await _seed()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _OP)
            .values(custom_description="")
        )
        await session.commit()

    result = await _search("gadget records")
    assert result["outcome"] == "matched"
    assert _hit(result)["description"] == _ORIGINAL
    assert (await _contract())["description"] == _ORIGINAL
