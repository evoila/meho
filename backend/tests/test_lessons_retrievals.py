# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search records the entries it gives to a session; only a reviewer confirms.

Covers :mod:`meho_backplane.lessons.retrievals` through the MCP handlers of
``search_knowledge`` and ``search_memory``, and the rule in
``add_to_knowledge`` that only a reviewer or a tenant admin may set
``status: confirmed``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import LessonRetrieval
from meho_backplane.kb.schemas import KbEntrySearchHit
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.server import McpPermissionDeniedError
from meho_backplane.mcp.tools.knowledge import (
    _add_to_knowledge_handler,
    _search_knowledge_handler,
)
from meho_backplane.mcp.tools.memory import _search_memory_handler
from meho_backplane.memory.schemas import MemoryEntrySearchHit, MemoryScope
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000057a1")
OWNER = uuid.UUID("00000000-0000-0000-0000-0000000057b2")
SESSION = uuid.UUID("00000000-0000-0000-0000-0000000057c3")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(OWNER))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _embedding() -> Iterator[None]:
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        yield


@pytest.fixture
def in_session() -> Iterator[None]:
    token = agent_session_id_var.set(SESSION)
    yield
    agent_session_id_var.reset(token)


def _operator(
    role: TenantRole = TenantRole.OPERATOR,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub="op-1",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=TENANT,
        tenant_role=role,
        principal_kind=kind,
    )


def _kb_hit(slug: str) -> KbEntrySearchHit:
    return KbEntrySearchHit(
        slug=slug,
        snippet="text",
        metadata={},
        fused_score=1.0,
        bm25_score=None,
        cosine_score=None,
        bm25_rank=None,
        cosine_rank=None,
    )


async def _rows() -> list[LessonRetrieval]:
    async with get_sessionmaker()() as session:
        return list((await session.scalars(select(LessonRetrieval))).all())


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_session")
async def test_search_knowledge_records_each_hit_with_status_and_expiry() -> None:
    service = KbService()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await service.create_entry(TENANT, "fresh", "fresh body", {"status": "confirmed"})
    await service.create_entry(
        TENANT, "old", "old body", {"status": "candidate", "expires_at": past}
    )
    hits = [_kb_hit("fresh"), _kb_hit("old"), _kb_hit("gone")]
    with patch.object(KbService, "search_entries", AsyncMock(return_value=hits)):
        response = await _search_knowledge_handler(_operator(), {"query": "body"})

    assert len(response["hits"]) == 3
    rows = {row.entry_id: row for row in await _rows()}
    fresh = await service.get_entry(TENANT, "fresh")
    old = await service.get_entry(TENANT, "old")
    assert fresh is not None and old is not None
    assert set(rows) == {fresh.id, old.id}, "a slug with no entry is not recorded"
    assert rows[fresh.id].status == "confirmed"
    assert rows[fresh.id].expired is False
    assert rows[old.id].status == "candidate"
    assert rows[old.id].expired is True
    assert all(row.agent_session_id == SESSION for row in rows.values())
    assert all(row.tenant_id == TENANT and row.store == "kb" for row in rows.values())


@pytest.mark.asyncio
async def test_a_search_without_a_session_records_nothing() -> None:
    await KbService().create_entry(TENANT, "fresh", "fresh body")
    with patch.object(KbService, "search_entries", AsyncMock(return_value=[_kb_hit("fresh")])):
        await _search_knowledge_handler(_operator(), {"query": "body"})
    assert await _rows() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_session")
async def test_a_slug_read_records_the_entry() -> None:
    entry, _ = await KbService().create_entry(TENANT, "runbook", "steps")
    await _search_knowledge_handler(_operator(), {"slug": "runbook"})
    rows = await _rows()
    assert [row.entry_id for row in rows] == [entry.id]


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_session")
async def test_a_shared_entry_is_recorded_with_its_owner_tenant() -> None:
    shared, _ = await KbService().create_entry(OWNER, "company-rule", "rule", {"shared": True})
    with patch.object(
        KbService, "search_entries", AsyncMock(return_value=[_kb_hit("company-rule")])
    ):
        await _search_knowledge_handler(_operator(), {"query": "rule"})
    (row,) = await _rows()
    assert row.entry_id == shared.id
    assert row.entry_tenant_id == OWNER
    assert row.tenant_id == TENANT


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_session")
async def test_the_own_entry_wins_over_a_shared_one_with_the_same_slug() -> None:
    own, _ = await KbService().create_entry(TENANT, "rule", "own rule")
    await KbService().create_entry(OWNER, "rule", "shared rule", {"shared": True})
    with patch.object(KbService, "search_entries", AsyncMock(return_value=[_kb_hit("rule")])):
        await _search_knowledge_handler(_operator(), {"query": "rule"})
    (row,) = await _rows()
    assert row.entry_id == own.id


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_session")
async def test_a_failed_record_does_not_fail_the_search() -> None:
    await KbService().create_entry(TENANT, "fresh", "fresh body")
    with (
        patch.object(KbService, "search_entries", AsyncMock(return_value=[_kb_hit("fresh")])),
        patch(
            "meho_backplane.lessons.retrievals.get_sessionmaker",
            side_effect=RuntimeError("database down"),
        ),
    ):
        response = await _search_knowledge_handler(_operator(), {"query": "body"})
    assert [hit["slug"] for hit in response["hits"]] == ["fresh"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("in_session")
async def test_search_memory_records_each_hit() -> None:
    from meho_backplane.memory.service import MemoryService

    operator = _operator(TenantRole.TENANT_ADMIN)
    entry = await MemoryService().remember(
        operator, scope=MemoryScope.TENANT, slug="note", body="a memory body"
    )
    now = datetime.now(UTC)
    hit = MemoryEntrySearchHit(
        rank=1,
        id=entry.id,
        scope=MemoryScope.TENANT,
        slug="note",
        user_sub=None,
        target_name=None,
        created_at=now,
        updated_at=now,
        expires_at=None,
        snippet="a memory",
        snippet_truncated=False,
        body_chars=13,
    )
    with patch.object(MemoryService, "search_memories", AsyncMock(return_value=[hit])):
        await _search_memory_handler(operator, {"query": "memory"})
    (row,) = await _rows()
    assert row.entry_id == entry.id
    assert row.store == "memory"


# --- only a reviewer confirms -------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_cannot_set_status_confirmed() -> None:
    agent = _operator(kind=PrincipalKind.AGENT)
    with pytest.raises(McpPermissionDeniedError) as caught:
        await _add_to_knowledge_handler(
            agent, {"slug": "lesson", "body": "x", "metadata": {"status": "confirmed"}}
        )
    assert caught.value.data == {"reason": "confirm_needs_reviewer", "slug": "lesson"}
    assert await KbService().get_entry(TENANT, "lesson") is None


@pytest.mark.asyncio
async def test_an_agent_cannot_confirm_its_own_candidate() -> None:
    agent = _operator(kind=PrincipalKind.AGENT)
    await _add_to_knowledge_handler(
        agent, {"slug": "lesson", "body": "x", "metadata": {"status": "candidate"}}
    )
    with pytest.raises(McpPermissionDeniedError):
        await _add_to_knowledge_handler(
            agent, {"slug": "lesson", "body": "x", "metadata": {"status": "confirmed"}}
        )
    stored = await KbService().get_entry(TENANT, "lesson")
    assert stored is not None
    assert stored.metadata["status"] == "candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator",
    [
        pytest.param(_operator(), id="human-operator-reviewer"),
        pytest.param(_operator(TenantRole.TENANT_ADMIN), id="tenant-admin"),
    ],
)
async def test_a_reviewer_or_tenant_admin_confirms_a_candidate(operator: Operator) -> None:
    agent = _operator(kind=PrincipalKind.AGENT)
    await _add_to_knowledge_handler(
        agent, {"slug": "lesson", "body": "x", "metadata": {"status": "candidate"}}
    )
    await _add_to_knowledge_handler(
        operator, {"slug": "lesson", "body": "x", "metadata": {"status": "confirmed"}}
    )
    stored = await KbService().get_entry(TENANT, "lesson")
    assert stored is not None
    assert stored.metadata["status"] == "confirmed"
