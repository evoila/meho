# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``SEARCH_PLAIN_SENTENCES`` switch of ``search_operations`` (#5490).

These tests run on SQLite. They check the switch, the outcome rules and
that the hit order never changes. The any-word check with PostgreSQL full
text search is in ``tests/integration/test_search_plain_sentences_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._search_plain_sentences import search_plain_sentences
from meho_backplane.operations.meta_tools import (
    SEARCH_NEAR_TIE_BAND,
    OperationSearchHit,
    search_operations,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import Settings, get_settings

_TENANT_A = UUID("00000000-0000-0000-0000-0000000005a0")
_TENANT_B = UUID("00000000-0000-0000-0000-0000000005b0")
_CONNECTOR_ID = "vault-1.x"
# Shares no word with any seeded operation, so the top hit has no keyword
# score and is only close in meaning.
_UNRELATED_QUERY = "zebra quartz fjord"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SEARCH_PLAIN_SENTENCES", raising=False)
    monkeypatch.delenv("AGENT_READS_CUSTOM_DESCRIPTION", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.fixture
def embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Every query and row has the same vector, so every row is a cosine hit."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "true")
    get_settings.cache_clear()


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


def _operator(tenant_id: UUID = _TENANT_A) -> Operator:
    return Operator(
        sub="op-plain-sentences",
        name="Plain Sentences Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_group(group_key: str) -> UUID:
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                group_key=group_key,
                name=group_key,
                when_to_use=f"Use it for {group_key}.",
                review_status="enabled",
            )
        )
    return group_id


async def _seed_op(
    op_id: str,
    *,
    group_id: UUID | None,
    enabled: bool = True,
    tenant_id: UUID | None = None,
) -> None:
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=op_id,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_search_plain_sentences._handler",
                summary=f"Operation {op_id}.",
                description="Reads keys.",
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=enabled,
                embedding=[0.1] * EMBEDDING_DIMENSION,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )


async def _search(query: str, group: str | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"connector_id": _CONNECTOR_ID, "query": query}
    if group is not None:
        arguments["group"] = group
    return await search_operations(_operator(), arguments)


def _hit(
    op_id: str, fused: float, bm25: float | None, summary: str | None = None
) -> OperationSearchHit:
    return OperationSearchHit(
        op_id=op_id,
        summary=summary,
        description=None,
        group_key=None,
        safety_level="safe",
        requires_approval=False,
        fused_score=fused,
        bm25_score=bm25,
        cosine_score=None,
    )


def _fake_search(monkeypatch: pytest.MonkeyPatch, hits: list[OperationSearchHit]) -> None:
    async def fake_hybrid_search(session: Any, **kwargs: Any) -> list[OperationSearchHit]:
        return hits

    monkeypatch.setattr("meho_backplane.operations.meta_tools.hybrid_search", fake_hybrid_search)


def test_switch_is_off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["search_plain_sentences"].default is False
    assert search_plain_sentences() is False
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")
    assert search_plain_sentences() is True


@pytest.mark.asyncio
async def test_one_operation_group_is_matched_only_with_the_switch(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    group_id = await _seed_group("object-collect")
    await _seed_op("vault.one", group_id=group_id)
    await _seed_op("vault.other", group_id=None)

    before = await _search(_UNRELATED_QUERY, group="object-collect")
    assert before["outcome"] == "ambiguous", before
    assert [h["op_id"] for h in before["hits"]] == ["vault.one"]

    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")
    after = await _search(_UNRELATED_QUERY, group="object-collect")
    assert after["outcome"] == "matched", after
    assert after["advisory"] is None
    assert after["narrowing"] == []
    assert [h["op_id"] for h in after["hits"]] == ["vault.one"]


@pytest.mark.asyncio
async def test_disabled_and_foreign_operations_do_not_count(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    """A disabled op and another tenant's op in the group leave one operation."""
    group_id = await _seed_group("kv")
    await _seed_op("vault.kv.list", group_id=group_id)
    await _seed_op("vault.kv.hidden", group_id=group_id, enabled=False)
    await _seed_op("vault.kv.foreign", group_id=group_id, tenant_id=_TENANT_B)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")

    result = await _search(_UNRELATED_QUERY, group="kv")
    assert [h["op_id"] for h in result["hits"]] == ["vault.kv.list"]
    assert result["outcome"] == "matched", result


@pytest.mark.asyncio
async def test_group_with_two_operations_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    group_id = await _seed_group("kv")
    await _seed_op("vault.kv.list", group_id=group_id)
    await _seed_op("vault.kv.read", group_id=group_id)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")

    result = await _search(_UNRELATED_QUERY, group="kv")
    assert len(result["hits"]) == 2
    assert result["outcome"] == "ambiguous", result


@pytest.mark.asyncio
async def test_two_operation_group_with_one_hit_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    """One hit is not enough: the group itself must hold one operation."""
    group_id = await _seed_group("kv")
    await _seed_op("vault.kv.list", group_id=group_id)
    await _seed_op("vault.kv.read", group_id=group_id)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")
    _fake_search(monkeypatch, [_hit("vault.kv.list", 0.05, None)])
    result = await _search(_UNRELATED_QUERY, group="kv")
    assert result["outcome"] == "ambiguous", result


@pytest.mark.asyncio
async def test_one_operation_connector_without_group_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    """The rule needs a ``group`` filter: without one, the agent can still narrow."""
    await _seed_op("vault.one", group_id=None)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")

    result = await _search(_UNRELATED_QUERY)
    assert [h["op_id"] for h in result["hits"]] == ["vault.one"]
    assert result["outcome"] == "ambiguous", result


@pytest.mark.asyncio
async def test_near_tie_stays_ambiguous_with_the_switch(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    """Two nearly tied hits with keyword scores still answer ``ambiguous``."""
    await _seed_op("vault.one", group_id=None)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")
    tied = [_hit("a", 0.05, 2.0), _hit("b", 0.05 - SEARCH_NEAR_TIE_BAND / 2, 1.0)]
    _fake_search(monkeypatch, tied)
    result = await _search("reboot a virtual machine")
    assert result["outcome"] == "ambiguous", result


_SENTENCE = "list the child paths that sit under one folder of the store"
_CLEAR_LEAD = [
    _hit("vault.kv.list", 0.05, None, summary="List the keys under a path."),
    _hit("vault.kv.read", 0.05 - 2 * SEARCH_NEAR_TIE_BAND, None, summary="Read a secret."),
]


@pytest.mark.asyncio
async def test_top_hit_with_a_query_word_is_matched_only_with_the_switch(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    """No keyword score, but the top hit holds "list": ``matched`` with the switch."""
    await _seed_op("vault.one", group_id=None)
    _fake_search(monkeypatch, _CLEAR_LEAD)
    assert len(_SENTENCE.split()) >= 8

    before = await _search(_SENTENCE)
    assert before["outcome"] == "ambiguous", before

    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")
    after = await _search(_SENTENCE)
    assert after["outcome"] == "matched", after
    assert [h["op_id"] for h in after["hits"]] == [h["op_id"] for h in before["hits"]]


@pytest.mark.asyncio
async def test_top_hit_without_a_query_word_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    await _seed_op("vault.one", group_id=None)
    _fake_search(monkeypatch, _CLEAR_LEAD)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")

    result = await _search(_UNRELATED_QUERY)
    assert result["outcome"] == "ambiguous", result


@pytest.mark.asyncio
async def test_top_hit_with_a_word_but_near_tie_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    await _seed_op("vault.one", group_id=None)
    tied = [
        _hit("vault.kv.list", 0.05, None, summary="List the keys under a path."),
        _hit("vault.kv.read", 0.05 - SEARCH_NEAR_TIE_BAND / 2, None, summary="Read a secret."),
    ]
    _fake_search(monkeypatch, tied)
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")

    result = await _search(_SENTENCE)
    assert result["outcome"] == "ambiguous", result


@pytest.mark.asyncio
async def test_top_hit_without_any_text_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    """A hit with no summary and no description holds no word."""
    await _seed_op("vault.one", group_id=None)
    _fake_search(monkeypatch, [_hit("vault.kv.list", 0.05, None)])
    _switch(monkeypatch, "SEARCH_PLAIN_SENTENCES")

    result = await _search(_SENTENCE)
    assert result["outcome"] == "ambiguous", result
