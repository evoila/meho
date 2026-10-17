# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :meth:`MemoryService.read_by_id` and the search-hit ``locator``.

The canonical selected read for a memory hit is
``meho://memory/id/{document_uuid}``. These tests pin the service side:

* every scope (including ``target`` and ``user-target``) reads back by id,
* a missing, deleted, foreign-tenant, other-owner, expired, non-memory,
  or broken row returns ``None`` (one answer for every miss, so the
  result is not an existence oracle),
* every search hit carries the canonical locator for its row.

Runs on the SQLite engine from :mod:`tests.conftest` with the embedding
service mocked, like :mod:`tests.test_memory_service`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.memory.schemas import MemoryScope, memory_locator
from meho_backplane.memory.service import MemoryService
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.retriever import RetrievalHit
from tests.test_memory_service import (  # noqa: F401 — fixtures are pytest-discovered
    _fake_embedding_service,
    _op,
    _required_settings_env,
)

pytestmark = pytest.mark.usefixtures("_fake_embedding_service")

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")


async def _set_metadata(document_id: uuid.UUID, metadata: dict[str, Any]) -> None:
    """Overwrite a row's ``doc_metadata`` to simulate a broken or tampered row."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(Document).where(Document.id == document_id).values(doc_metadata=metadata)
        )
        await session.commit()


@pytest.mark.parametrize(
    "scope, target_name",
    [
        (MemoryScope.USER, None),
        (MemoryScope.USER_TENANT, None),
        (MemoryScope.USER_TARGET, "infra-1"),
        (MemoryScope.TENANT, None),
        (MemoryScope.TARGET, "infra-1"),
    ],
)
@pytest.mark.asyncio
async def test_read_by_id_returns_entry_for_every_scope(
    scope: MemoryScope,
    target_name: str | None,
) -> None:
    """Every scope reads back by id, including both target-flavoured scopes."""
    role = (
        TenantRole.TENANT_ADMIN
        if scope in {MemoryScope.TENANT, MemoryScope.TARGET}
        else TenantRole.OPERATOR
    )
    writer = _op(role=role, tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(
        operator=writer, scope=scope, body="read me by id", target_name=target_name
    )

    entry = await service.read_by_id(writer, stored.id)

    assert entry is not None
    assert entry.id == stored.id
    assert entry.scope is scope
    assert entry.target_name == target_name
    assert entry.body == "read me by id"


@pytest.mark.asyncio
async def test_read_by_id_lets_any_tenant_operator_read_shared_target_row() -> None:
    """A ``target`` row is tenant-shared: another operator in the tenant reads it."""
    admin = _op(sub="admin", role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT)
    reader = _op(sub="reader", role=TenantRole.READ_ONLY, tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(
        operator=admin, scope=MemoryScope.TARGET, body="shared", target_name="infra-1"
    )

    entry = await service.read_by_id(reader, stored.id)

    assert entry is not None
    assert entry.target_name == "infra-1"


@pytest.mark.asyncio
async def test_read_by_id_hides_other_operators_user_target_row() -> None:
    """Knowing the id of another operator's user-target row grants nothing."""
    alice = _op(sub="alice", tenant_id=_TENANT)
    bob = _op(sub="bob", tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(
        operator=alice, scope=MemoryScope.USER_TARGET, body="private", target_name="infra-1"
    )

    assert await service.read_by_id(bob, stored.id) is None
    assert await service.read_by_id(alice, stored.id) is not None


@pytest.mark.asyncio
async def test_read_by_id_hides_foreign_tenant_row() -> None:
    """A row in another tenant is invisible even with the right id."""
    admin_a = _op(sub="admin-a", role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT)
    admin_b = _op(
        sub="admin-b",
        role=TenantRole.TENANT_ADMIN,
        tenant_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    )
    service = MemoryService()
    stored = await service.remember(operator=admin_a, scope=MemoryScope.TENANT, body="tenant a")

    assert await service.read_by_id(admin_b, stored.id) is None


@pytest.mark.asyncio
async def test_read_by_id_hides_expired_row() -> None:
    """An expired row reads as missing, same as :meth:`MemoryService.recall`."""
    operator = _op(tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="stale",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    assert await service.read_by_id(operator, stored.id) is None


@pytest.mark.asyncio
async def test_read_by_id_returns_none_for_unknown_and_deleted_ids() -> None:
    """A random id and a forgotten row's id both read as missing."""
    operator = _op(tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(operator=operator, scope=MemoryScope.USER, body="gone soon")
    assert await service.forget(operator, scope=MemoryScope.USER, slug=stored.slug)

    assert await service.read_by_id(operator, stored.id) is None
    assert await service.read_by_id(operator, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_read_by_id_ignores_non_memory_rows() -> None:
    """A kb row in the same tenant is not a memory, so its id reads as missing."""
    operator = _op(tenant_id=_TENANT)
    doc = await index_document(
        tenant_id=_TENANT,
        source="kb",
        source_id="kb-note",
        kind="note",
        body="knowledge base row",
    )

    assert await MemoryService().read_by_id(operator, doc.id) is None


@pytest.mark.asyncio
async def test_read_by_id_rejects_target_row_without_target_name() -> None:
    """A target row whose metadata lost its target name is treated as missing."""
    admin = _op(role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(
        operator=admin, scope=MemoryScope.TARGET, body="broken", target_name="infra-1"
    )
    await _set_metadata(stored.id, {"scope": "target", "target_name": None})

    assert await service.read_by_id(admin, stored.id) is None


@pytest.mark.asyncio
async def test_read_by_id_rejects_owner_mismatch_between_key_and_metadata() -> None:
    """The owner is pinned by the natural key, not only by metadata.

    Alice's row is relabelled in metadata as Bob's. Alice fails the RBAC
    check (metadata owner is Bob) and Bob fails the natural-key check
    (the key still says Alice), so neither can read it by id.
    """
    alice = _op(sub="alice", tenant_id=_TENANT)
    bob = _op(sub="bob", tenant_id=_TENANT)
    service = MemoryService()
    stored = await service.remember(operator=alice, scope=MemoryScope.USER, body="mine")
    await _set_metadata(stored.id, {"scope": "user", "user_sub": "bob"})

    assert await service.read_by_id(alice, stored.id) is None
    assert await service.read_by_id(bob, stored.id) is None


@pytest.mark.asyncio
async def test_search_hits_carry_canonical_locator() -> None:
    """Every search hit carries ``meho://memory/id/{document_uuid}`` for its row."""
    operator = _op(tenant_id=_TENANT)
    ts = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    document_id = uuid.uuid4()
    hit = RetrievalHit(
        document_id=document_id,
        tenant_id=_TENANT,
        source="memory",
        source_id="target:infra-1:disk-note",
        kind="memory-target",
        body="disk note",
        doc_metadata={"scope": "target", "target_name": "infra-1", "user_sub": None},
        created_at=ts,
        updated_at=ts,
        fused_score=0.5,
        bm25_score=0.5,
        cosine_score=0.5,
        bm25_rank=1,
        cosine_rank=1,
    )

    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(return_value=[hit]),
    ):
        results = await MemoryService().search_memories(operator, "disk", scope=MemoryScope.TARGET)

    assert len(results) == 1
    expected = f"meho://memory/id/{document_id}"
    assert results[0].locator == expected
    assert memory_locator(document_id) == expected
    dumped = results[0].model_dump(mode="json")
    assert dumped["locator"] == expected
    # The locator names the row only; no target name or owner leaks into it.
    assert "infra-1" not in dumped["locator"]
