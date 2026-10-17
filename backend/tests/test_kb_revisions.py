# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retained kb revisions through :class:`KbService` (#3753).

Checks that:

* an overwrite keeps the earlier body readable and byte-identical;
* an unchanged body does not add a revision;
* the latest read (``get_entry`` / ``list_entries``) stays the head and
  names its current revision;
* the exact read hides foreign, mismatched and unknown ids;
* delete removes the head and every revision together, and a failed
  delete leaves both readable.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, KbRevision
from meho_backplane.kb.revisions import CHUNKER_VERSION
from meho_backplane.kb.service import KbService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_INGEST_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_embedding() -> Iterator[None]:
    fake = AsyncMock()
    fake.encode_one.return_value = [0.1] * 384
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        yield


async def _revision_count(entry_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count()).select_from(KbRevision).where(KbRevision.entry_id == entry_id)
        )
    return int(count or 0)


@pytest.mark.asyncio
async def test_overwrite_keeps_earlier_revision_readable_and_byte_identical() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    first_body = "Restart the pod.\n\nUse `kubectl delete pod` — then wait. ✓"
    first, created = await service.create_entry(tenant, "pod-restart", first_body)
    assert created is True
    assert first.revision_id is not None

    second, _ = await service.create_entry(tenant, "pod-restart", "Use a rollout restart.")
    assert second.id == first.id
    assert second.revision_id is not None
    assert second.revision_id != first.revision_id

    old = await service.get_revision(tenant, first.id, first.revision_id)
    assert old is not None
    assert old.body == first_body
    assert old.body.encode("utf-8") == first_body.encode("utf-8")
    assert old.body_hash == hashlib.sha256(first_body.encode("utf-8")).hexdigest()
    assert old.revision_number == 1
    assert old.origin == "write"
    assert old.slug == "pod-restart"
    assert old.embedding_model == get_settings().retrieval_embedding_model
    assert old.chunker_version == CHUNKER_VERSION
    assert old.provenance.state == "recorded"
    assert old.provenance.source_kind == "direct"

    new = await service.get_revision(tenant, second.id, second.revision_id)
    assert new is not None
    assert new.revision_number == 2
    assert new.body == "Use a rollout restart."


@pytest.mark.asyncio
async def test_latest_reads_stay_on_the_head_and_name_its_revision() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    await service.create_entry(tenant, "dns-check", "old body")
    latest, _ = await service.create_entry(tenant, "dns-check", "new body")

    shown = await service.get_entry(tenant, "dns-check")
    assert shown is not None
    assert shown.body == "new body"
    assert shown.revision_id == latest.revision_id

    listed = await service.list_entries(tenant)
    assert [(e.slug, e.revision_id) for e in listed] == [("dns-check", latest.revision_id)]


@pytest.mark.asyncio
async def test_unchanged_body_reuses_the_current_revision() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    first, _ = await service.create_entry(tenant, "same-body", "body")
    again, _ = await service.create_entry(tenant, "same-body", "body", {"tag": "x"})
    assert again.revision_id == first.revision_id
    assert await _revision_count(first.id) == 1


@pytest.mark.asyncio
async def test_returning_to_an_old_body_adds_a_new_revision() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    one, _ = await service.create_entry(tenant, "flip", "A")
    await service.create_entry(tenant, "flip", "B")
    three, _ = await service.create_entry(tenant, "flip", "A")
    assert three.revision_id != one.revision_id
    assert three.revision_id is not None
    revision = await service.get_revision(tenant, three.id, three.revision_id)
    assert revision is not None
    assert revision.revision_number == 3


@pytest.mark.asyncio
async def test_directory_ingest_stores_revisions_with_directory_provenance(
    tmp_path: Path,
) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    corpus = tmp_path / "kb"
    corpus.mkdir()
    (corpus / "vpn-reset.md").write_text("first", encoding="utf-8")
    await service.ingest_directory(corpus, tenant)
    first = await service.get_entry(tenant, "vpn-reset")
    assert first is not None
    assert first.revision_id is not None

    await service.ingest_directory(corpus, tenant)
    assert await _revision_count(first.id) == 1

    (corpus / "vpn-reset.md").write_text("second", encoding="utf-8")
    await service.ingest_directory(corpus, tenant)
    assert await _revision_count(first.id) == 2

    old = await service.get_revision(tenant, first.id, first.revision_id)
    assert old is not None
    assert old.body == "first"
    assert old.provenance.source_kind == "directory"
    assert old.provenance.relative_path == "vpn-reset.md"


@pytest.mark.asyncio
async def test_exact_read_hides_foreign_mismatched_and_unknown_ids() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    entry, _ = await service.create_entry(tenant, "owned", "body")
    other, _ = await service.create_entry(tenant, "other", "other body")
    assert entry.revision_id is not None
    assert other.revision_id is not None

    assert await service.get_revision(other_tenant, entry.id, entry.revision_id) is None
    assert await service.get_revision(tenant, other.id, entry.revision_id) is None
    assert await service.get_revision(tenant, entry.id, uuid.uuid4()) is None
    assert await service.get_revision(tenant, uuid.uuid4(), entry.revision_id) is None


@pytest.mark.asyncio
async def test_delete_removes_head_and_every_revision() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    first, _ = await service.create_entry(tenant, "to-delete", "v1")
    second, _ = await service.create_entry(tenant, "to-delete", "v2")
    keep, _ = await service.create_entry(tenant, "to-keep", "stays")
    assert first.revision_id is not None
    assert second.revision_id is not None

    assert await service.delete_entry(tenant, "to-delete") is True

    assert await service.get_entry(tenant, "to-delete") is None
    assert await service.get_revision(tenant, first.id, first.revision_id) is None
    assert await service.get_revision(tenant, second.id, second.revision_id) is None
    assert await _revision_count(first.id) == 0
    async with get_sessionmaker()() as session:
        assert await session.get(Document, first.id) is None
    assert await _revision_count(keep.id) == 1

    # A new entry with the same slug starts a fresh history.
    reborn, _ = await service.create_entry(tenant, "to-delete", "v1")
    assert reborn.id != first.id
    assert reborn.revision_id is not None
    revision = await service.get_revision(tenant, reborn.id, reborn.revision_id)
    assert revision is not None
    assert revision.revision_number == 1


@pytest.mark.asyncio
async def test_failed_delete_leaves_head_and_revisions_readable() -> None:
    service = KbService()
    tenant = uuid.uuid4()
    entry, _ = await service.create_entry(tenant, "kept-on-error", "body")
    assert entry.revision_id is not None

    with (
        patch.object(AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("db down"))),
        pytest.raises(RuntimeError, match="db down"),
    ):
        await service.delete_entry(tenant, "kept-on-error")

    assert await service.get_entry(tenant, "kept-on-error") is not None
    assert await service.get_revision(tenant, entry.id, entry.revision_id) is not None
