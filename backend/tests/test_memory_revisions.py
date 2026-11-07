# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory entries keep revisions and carry the ``source`` header field."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import MemoryRevision
from meho_backplane.lesson_header import LessonHeaderError
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.settings import get_settings

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fake_embedding() -> Iterator[None]:
    fake = AsyncMock()
    fake.encode_one.return_value = [0.01] * 384
    fake.dimension = 384
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        yield


def _op(sub: str = "op-1", role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=TENANT,
        tenant_role=role,
    )


async def _count() -> int:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(func.count()).select_from(MemoryRevision))
        ).scalar_one()


@pytest.mark.asyncio
async def test_each_change_keeps_a_revision() -> None:
    service, op = MemoryService(), _op()
    first = await service.remember(op, MemoryScope.USER, "v1", slug="note")
    await service.remember(op, MemoryScope.USER, "v2", slug="note")
    await service.remember(op, MemoryScope.USER, "v2", slug="note", metadata={"summary": "s"})

    revisions = await service.revisions(op, first.id)
    assert revisions is not None
    assert [r.revision_number for r in revisions] == [1, 2, 3]
    assert [r.body for r in revisions] == ["v1", "v2", "v2"]
    assert revisions[2].metadata["summary"] == "s"
    assert all(r.written_by_sub == "op-1" for r in revisions)


@pytest.mark.asyncio
async def test_a_write_that_changes_nothing_keeps_no_revision() -> None:
    service, op = MemoryService(), _op()
    await service.remember(op, MemoryScope.USER, "same", slug="note")
    await service.remember(op, MemoryScope.USER, "same", slug="note")
    assert await _count() == 1


@pytest.mark.asyncio
async def test_revisions_of_another_users_memory_read_as_none() -> None:
    service = MemoryService()
    entry = await service.remember(_op("op-1"), MemoryScope.USER, "mine", slug="note")
    assert await service.revisions(_op("op-2"), entry.id) is None
    assert await service.revisions(_op("op-1"), uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_forget_removes_the_revisions() -> None:
    service, op = MemoryService(), _op()
    await service.remember(op, MemoryScope.USER, "v1", slug="note")
    await service.remember(op, MemoryScope.USER, "v2", slug="note")
    assert await _count() == 2
    assert await service.forget(op, MemoryScope.USER, "note") is True
    assert await _count() == 0


@pytest.mark.asyncio
async def test_source_is_a_field_on_the_entry() -> None:
    service, op = MemoryService(), _op()
    entry = await service.remember(
        op, MemoryScope.USER, "note", slug="n", metadata={"source": "lab field note"}
    )
    assert entry.source == "lab field note"
    plain = await service.remember(op, MemoryScope.USER, "note", slug="plain")
    assert plain.source is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [{"type": "gossip"}, {"status": "done"}, {"learned_at": "soon"}, {"kind": "x"}, {"slug": "y"}],
)
async def test_a_wrong_header_is_refused_and_nothing_is_written(metadata: dict[str, str]) -> None:
    service, op = MemoryService(), _op()
    with pytest.raises(LessonHeaderError):
        await service.remember(op, MemoryScope.USER, "note", slug="bad", metadata=metadata)
    assert await service.recall(op, MemoryScope.USER, "bad") is None
    assert await _count() == 0


@pytest.mark.asyncio
async def test_memory_keeps_its_own_scope_name_in_the_metadata() -> None:
    service, op = MemoryService(), _op()
    entry = await service.remember(
        op, MemoryScope.USER, "note", slug="n", metadata={"scope": "tenant", "type": "fact"}
    )
    assert entry.metadata["scope"] == "user"
    assert entry.metadata["type"] == "fact"
