# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Memory entries keep revisions and carry the ``source`` header field."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import MemoryRevision
from meho_backplane.kb.service import KbService
from meho_backplane.lesson_header import LessonHeaderError
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.memory.ttl import resolve_default_expires_at
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

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
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
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


@pytest.mark.asyncio
async def test_a_failed_revision_write_keeps_the_old_version() -> None:
    service, op = MemoryService(), _op()
    await service.remember(op, MemoryScope.USER, "v1", slug="note")
    with (
        patch(
            "meho_backplane.memory.revisions.add_revision",
            side_effect=RuntimeError("revision store failed"),
        ),
        pytest.raises(RuntimeError),
    ):
        await service.remember(op, MemoryScope.USER, "v2", slug="note")

    entry = await service.recall(op, MemoryScope.USER, "note")
    assert entry is not None
    assert entry.body == "v1"
    revisions = await service.revisions(op, entry.id)
    assert revisions is not None
    assert [r.body for r in revisions] == ["v1"]


@pytest.mark.asyncio
async def test_concurrent_writes_keep_every_version_in_order() -> None:
    service, op = MemoryService(), _op()
    await service.remember(op, MemoryScope.USER, "v0", slug="note")
    await asyncio.gather(
        *(service.remember(op, MemoryScope.USER, f"v{i}", slug="note") for i in range(1, 6))
    )

    entry = await service.recall(op, MemoryScope.USER, "note")
    assert entry is not None
    revisions = await service.revisions(op, entry.id)
    assert revisions is not None
    assert [r.revision_number for r in revisions] == [1, 2, 3, 4, 5, 6]
    assert sorted(r.body for r in revisions) == [f"v{i}" for i in range(6)]
    # The newest revision is the body the entry holds now.
    assert revisions[-1].body == entry.body


@pytest.mark.asyncio
async def test_a_promotion_stores_the_revision_with_the_new_entry() -> None:
    service, op = MemoryService(), _op()
    await service.remember(op, MemoryScope.USER, "note body", slug="note")
    promoted = await service.promote(
        op,
        source_scope=MemoryScope.USER,
        source_slug="note",
        target_scope=MemoryScope.USER_TENANT,
        move=True,
    )
    assert promoted is not None
    revisions = await service.revisions(op, promoted.id)
    assert revisions is not None
    assert [r.body for r in revisions] == ["note body"]
    assert await _count() == 1


@pytest.mark.asyncio
async def test_a_failed_promotion_stores_no_revision() -> None:
    service, op = MemoryService(), _op()
    await service.remember(op, MemoryScope.USER, "note body", slug="note")
    with (
        patch(
            "meho_backplane.memory.service.add_revision",
            side_effect=RuntimeError("revision store failed"),
        ),
        pytest.raises(RuntimeError),
    ):
        await service.promote(
            op,
            source_scope=MemoryScope.USER,
            source_slug="note",
            target_scope=MemoryScope.USER_TENANT,
            move=True,
        )

    assert await service.recall(op, MemoryScope.USER, "note") is not None
    assert await service.recall(op, MemoryScope.USER_TENANT, "note") is None
    assert await _count() == 1


_HEADER_EXPIRY = "2027-05-01T00:00:00+00:00"


@pytest.mark.parametrize("scope", [MemoryScope.USER, MemoryScope.TENANT])
def test_the_header_expiry_is_used_when_no_expiry_is_given(scope: MemoryScope) -> None:
    resolved = resolve_default_expires_at(
        scope,
        expires_at_was_set=False,
        explicit_expires_at=None,
        metadata={"expires_at": "2027-05-01"},
    )
    assert resolved == datetime(2027, 5, 1, tzinfo=UTC)


def test_an_explicit_expiry_wins_over_the_header() -> None:
    explicit = datetime(2026, 12, 1, tzinfo=UTC)
    metadata = {"expires_at": _HEADER_EXPIRY}
    assert (
        resolve_default_expires_at(
            MemoryScope.USER,
            expires_at_was_set=True,
            explicit_expires_at=explicit,
            metadata=metadata,
        )
        == explicit
    )
    # An explicit null (keep forever) also wins.
    assert (
        resolve_default_expires_at(
            MemoryScope.USER,
            expires_at_was_set=True,
            explicit_expires_at=None,
            metadata=metadata,
        )
        is None
    )


def test_without_a_header_expiry_the_default_lifetime_applies() -> None:
    before = datetime.now(UTC)
    resolved = resolve_default_expires_at(
        MemoryScope.USER,
        expires_at_was_set=False,
        explicit_expires_at=None,
        metadata={"type": "fact"},
    )
    assert resolved is not None
    assert before < resolved < before + timedelta(days=366)
    # A value that is not a date is left to the header check.
    assert (
        resolve_default_expires_at(
            MemoryScope.TENANT,
            expires_at_was_set=False,
            explicit_expires_at=None,
            metadata={"expires_at": "soon"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_header_expiry_moves_unchanged_between_knowledge_and_memory() -> None:
    header = {"type": "lesson", "status": "candidate", "expires_at": _HEADER_EXPIRY}
    kb_entry, _ = await KbService().create_entry(
        TENANT, "moved-note", "note", metadata=dict(header), actor_sub="op-1"
    )
    op = _op()
    expires_at = resolve_default_expires_at(
        MemoryScope.USER,
        expires_at_was_set=False,
        explicit_expires_at=None,
        metadata=kb_entry.metadata,
    )
    memory_entry = await MemoryService().remember(
        op,
        MemoryScope.USER,
        "note",
        slug="moved-note",
        metadata={k: kb_entry.metadata[k] for k in header},
        expires_at=expires_at,
    )
    assert kb_entry.metadata["expires_at"] == _HEADER_EXPIRY
    assert memory_entry.metadata["expires_at"] == _HEADER_EXPIRY
    assert memory_entry.expires_at == datetime(2027, 5, 1, tzinfo=UTC)
