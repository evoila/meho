# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The lesson header and the shared scope on knowledge entries.

Covers the header check on direct create and on directory import, the
default expiry of a candidate, and the shared scope: readable from a
second tenant, never writable from it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.kb.schemas import KbSharedNotAllowedError, KbSharedSlugError
from meho_backplane.kb.service import KbService
from meho_backplane.lesson_header import LessonHeaderError
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
THIRD = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_INGEST_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(OWNER))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding() -> Iterator[AsyncMock]:
    fake_service = fake_embedding_service()
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_service,
    ):
        yield fake_service.encode


# --- the header check ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_note_with_the_header_shows_every_field_on_read(stub_embedding: AsyncMock) -> None:
    header: dict[str, Any] = {
        "type": "lesson",
        "applies_to": "product-and-version",
        "status": "confirmed",
        "product": "vcenter",
        "version": "9.0",
        "source": "team wiki",
        "author": "ops",
        "learned_at": "2026-11-01T00:00:00+00:00",
        "confirmed_at": "2026-11-02T00:00:00+00:00",
        "tags": ["vm"],
        "summary": "Snapshots block a clone.",
    }
    service = KbService()
    await service.create_entry(OWNER, "snap-lesson", "body", header)
    entry = await service.get_entry(OWNER, "snap-lesson")
    assert entry is not None
    assert {k: entry.metadata[k] for k in header} == header
    listed = await service.list_entries(OWNER)
    assert {k: listed[0].metadata[k] for k in header} == header


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"type": "story"},
        {"applies_to": "everyone"},
        {"status": "done"},
        {"learned_at": "last week"},
        {"kind": "x"},
        {"scope": "tenant"},
        {"slug": "other"},
    ],
)
async def test_a_wrong_header_is_refused_and_nothing_is_written(
    stub_embedding: AsyncMock, metadata: dict[str, Any]
) -> None:
    service = KbService()
    with pytest.raises(LessonHeaderError):
        await service.create_entry(OWNER, "bad", "body", metadata)
    assert await service.get_entry(OWNER, "bad") is None


@pytest.mark.asyncio
async def test_a_candidate_without_expires_at_gets_six_months(stub_embedding: AsyncMock) -> None:
    entry, _ = await KbService().create_entry(
        OWNER, "cand", "body", {"status": "candidate", "learned_at": "2026-11-01"}
    )
    assert entry.metadata["expires_at"] == "2027-05-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_a_confirmed_entry_gets_no_expiry(stub_embedding: AsyncMock) -> None:
    entry, _ = await KbService().create_entry(OWNER, "conf", "body", {"status": "confirmed"})
    assert "expires_at" not in entry.metadata


@pytest.mark.asyncio
async def test_a_rewrite_of_a_candidate_keeps_its_dates(stub_embedding: AsyncMock) -> None:
    service = KbService()
    first, _ = await service.create_entry(OWNER, "cand", "v1", {"status": "candidate"})
    second, _ = await service.create_entry(OWNER, "cand", "v2", {"status": "candidate"})
    assert second.metadata["learned_at"] == first.metadata["learned_at"]
    assert second.metadata["expires_at"] == first.metadata["expires_at"]


@pytest.mark.asyncio
async def test_import_refuses_a_wrong_header_and_keeps_the_good_files(
    tmp_path: Path, stub_embedding: AsyncMock
) -> None:
    (tmp_path / "good.md").write_text("---\ntype: fact\nstatus: candidate\n---\nGood note.")
    (tmp_path / "bad.md").write_text("---\ntype: gossip\n---\nBad note.")
    (tmp_path / "dated.md").write_text("---\nstatus: confirmed\nlearned_at: 2026-11-03\n---\nD.")
    service = KbService()
    result = await service.ingest_directory(tmp_path, OWNER)
    assert result.inserted_count == 2
    assert result.error_count == 1
    assert "bad.md" in result.errors[0]
    assert "type must be one of" in result.errors[0]
    good = await service.get_entry(OWNER, "good")
    assert good is not None
    assert good.metadata["type"] == "fact"
    assert "expires_at" in good.metadata
    dated = await service.get_entry(OWNER, "dated")
    assert dated is not None
    assert dated.metadata["learned_at"] == "2026-11-03T00:00:00+00:00"
    assert "expires_at" not in dated.metadata


@pytest.mark.asyncio
async def test_re_import_of_a_candidate_changes_nothing(
    tmp_path: Path, stub_embedding: AsyncMock
) -> None:
    (tmp_path / "cand.md").write_text("---\nstatus: candidate\n---\nA candidate.")
    service = KbService()
    await service.ingest_directory(tmp_path, OWNER)
    first = await service.get_entry(OWNER, "cand")
    result = await service.ingest_directory(tmp_path, OWNER)
    second = await service.get_entry(OWNER, "cand")
    assert result.skipped_count == 1
    assert first is not None and second is not None
    assert second.metadata["expires_at"] == first.metadata["expires_at"]


# --- the shared scope ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_shared_note_is_readable_from_a_second_tenant(stub_embedding: AsyncMock) -> None:
    service = KbService()
    await service.create_entry(OWNER, "lab-note", "generic note", {"shared": True})
    await service.create_entry(OWNER, "private-note", "owner only")

    seen = await service.get_entry(OTHER, "lab-note")
    assert seen is not None
    assert seen.body == "generic note"
    assert seen.tenant_id == OWNER
    assert await service.get_entry(OTHER, "private-note") is None
    assert [e.slug for e in await service.list_entries(OTHER)] == ["lab-note"]
    assert [e.slug for e in await service.list_entries(THIRD)] == ["lab-note"]


@pytest.mark.asyncio
async def test_a_second_tenant_cannot_write_the_shared_slug(stub_embedding: AsyncMock) -> None:
    service = KbService()
    await service.create_entry(OWNER, "lab-note", "generic note", {"shared": True})
    with pytest.raises(KbSharedSlugError) as exc:
        await service.create_entry(OTHER, "lab-note", "my version")
    assert "Use a different slug" in str(exc.value)
    assert await service.delete_entry(OTHER, "lab-note") is False
    kept = await service.get_entry(OWNER, "lab-note")
    assert kept is not None
    assert kept.body == "generic note"


@pytest.mark.asyncio
async def test_a_tenant_that_is_not_listed_cannot_share(stub_embedding: AsyncMock) -> None:
    service = KbService()
    with pytest.raises(KbSharedNotAllowedError):
        await service.create_entry(OTHER, "mine", "body", {"shared": True})
    assert await service.get_entry(OTHER, "mine") is None
    await service.create_entry(OTHER, "mine", "body", {"shared": False})
    assert await service.get_entry(THIRD, "mine") is None


@pytest.mark.asyncio
async def test_shared_must_be_a_boolean(stub_embedding: AsyncMock) -> None:
    with pytest.raises(LessonHeaderError, match="shared"):
        await KbService().create_entry(OWNER, "x", "body", {"shared": "yes"})


@pytest.mark.asyncio
async def test_unsharing_hides_the_note_again(stub_embedding: AsyncMock) -> None:
    service = KbService()
    await service.create_entry(OWNER, "lab-note", "generic note", {"shared": True})
    await service.create_entry(OWNER, "lab-note", "generic note", {"shared": False})
    assert await service.get_entry(OTHER, "lab-note") is None


@pytest.mark.asyncio
async def test_nothing_is_shared_when_the_owner_is_not_listed(
    stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = KbService()
    await service.create_entry(OWNER, "lab-note", "generic note", {"shared": True})
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", "")
    get_settings.cache_clear()
    assert await service.get_entry(OTHER, "lab-note") is None


@pytest.mark.asyncio
async def test_a_bad_tenant_id_in_the_setting_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", "not-a-uuid")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        _ = get_settings().kb_shared_tenant_set


@pytest.mark.asyncio
async def test_import_of_a_shared_note_and_of_a_taken_slug(
    tmp_path: Path, stub_embedding: AsyncMock
) -> None:
    (tmp_path / "lab-note.md").write_text("---\nshared: true\n---\nGeneric note.")
    service = KbService()
    owner_result = await service.ingest_directory(tmp_path, OWNER)
    assert owner_result.inserted_count == 1
    other_result = await service.ingest_directory(tmp_path, OTHER)
    assert other_result.error_count == 1
    assert other_result.inserted_count == 0


def _hit(tenant_id: uuid.UUID, slug: str, score: float) -> RetrievalHit:
    ts = datetime(2026, 11, 7, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="kb",
        source_id=slug,
        kind="kb-entry",
        body=f"body of {slug}",
        doc_metadata={},
        created_at=ts,
        updated_at=ts,
        fused_score=score,
        bm25_score=None,
        cosine_score=score,
        bm25_rank=None,
        cosine_rank=1,
    )


@pytest.mark.asyncio
async def test_search_adds_the_shared_hits_and_asks_only_for_shared_rows() -> None:
    calls: list[dict[str, Any]] = []

    async def fake_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        calls.append(kwargs)
        if kwargs["tenant_id"] == OWNER:
            return [_hit(OWNER, "lab-note", 0.9), _hit(OWNER, "own-twin", 0.5)]
        return [_hit(OTHER, "own-twin", 0.7), _hit(OTHER, "mine", 0.4)]

    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        hits = await KbService().search_entries(OTHER, "snapshot", filters={"team": "x"})

    # The tenant's own entry wins over a shared one with the same slug.
    assert [h.slug for h in hits] == ["lab-note", "own-twin", "mine"]
    own_call = next(c for c in calls if c["tenant_id"] == OTHER)
    shared_call = next(c for c in calls if c["tenant_id"] == OWNER)
    assert own_call["metadata_filters"] == {"team": "x"}
    assert shared_call["metadata_filters"] == {"team": "x", "shared": True}


@pytest.mark.asyncio
async def test_the_owner_tenant_searches_only_itself() -> None:
    calls: list[uuid.UUID] = []

    async def fake_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        calls.append(kwargs["tenant_id"])
        return []

    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        await KbService().search_entries(OWNER, "snapshot")
    assert calls == [OWNER]


@pytest.mark.asyncio
async def test_a_shared_revision_is_readable_from_a_second_tenant(
    stub_embedding: AsyncMock,
) -> None:
    service = KbService()
    entry, _ = await service.create_entry(OWNER, "lab-note", "generic note", {"shared": True})
    assert entry.revision_id is not None
    seen = await service.get_revision(OTHER, entry.id, entry.revision_id)
    assert seen is not None
    assert seen.body == "generic note"
    private, _ = await service.create_entry(OWNER, "private", "secret")
    assert private.revision_id is not None
    assert await service.get_revision(OTHER, private.id, private.revision_id) is None


@pytest.mark.asyncio
async def test_only_the_json_true_counts_as_shared(stub_embedding: AsyncMock) -> None:
    """An old row with some other value under ``shared`` is not shared and breaks nothing."""
    from meho_backplane.retrieval.indexer import index_document

    await index_document(
        tenant_id=OWNER,
        source="kb",
        source_id="odd",
        kind="kb-entry",
        body="odd",
        metadata={"shared": "maybe"},
    )
    service = KbService()
    assert await service.get_entry(OTHER, "odd") is None
    assert await service.list_entries(OTHER) == []


def test_the_postgresql_test_for_shared_has_no_cast() -> None:
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from meho_backplane.db.models import Document
    from meho_backplane.kb.shared import visible_to

    sql = str(select(Document.id).where(visible_to(OTHER)).compile(dialect=postgresql.dialect()))
    assert "'true'::jsonb" in sql
    assert "CAST" not in sql.upper()
