# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.kb.service`.

Coverage matrix (G4.1-T1 / #415 acceptance criteria):

* ``ingest_directory`` first run → inserts every file; second run
  against unchanged corpus → all skipped (body-hash short-circuit).
* Body change between runs → one ``updated``, rest ``skipped``.
* ``dry_run=True`` classifies actions without writing.
* Tenant boundary: ingesting under tenant A does not surface for
  tenant B's ``list_entries`` / ``get_entry``.
* ``list_entries`` returns slug-sorted entries; tenant-scoped;
  ``filter_pattern`` narrows.
* ``get_entry`` returns the full body; ``None`` for unknown slug.
* ``create_entry`` writes a new entry; subsequent ``get_entry``
  returns it; slug validation rejects bad input.
* ``delete_entry`` removes the row, returns ``True``; ``False`` for
  unknown slug.
* ``search_entries`` adapts retrieve hits to ``KbEntrySearchHit``
  (slug instead of source_id; snippet truncated at the documented
  width); pinned to ``source='kb'``.
* Error path: invalid slug + binary file are counted, not raised.

Embedding is mocked via the same singleton-patching pattern
:mod:`tests.test_retrieval_indexer` established so SQLite tests
don't pull fastembed or ONNX runtime. The SQLite path is the load-
bearing test driver -- the unique composite index +
:class:`Document` FK to ``tenant`` are both honoured by the SQLite
engine. PG-real coverage of the search + BM25 ranking lives in
``tests/integration/test_kb_service_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.kb.service import KbService
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars the :class:`Settings` model requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding() -> Iterator[AsyncMock]:
    """Patch :func:`get_embedding_service` so the indexer encodes deterministically.

    Same pattern :mod:`tests.test_retrieval_indexer` uses -- patches
    the import site inside the indexer module so the embed singleton
    factory is bypassed entirely.
    """
    fake_service = AsyncMock()
    fake_service.encode_one.return_value = [0.1] * 384
    fake_service.encode.return_value = [[0.1] * 384]
    fake_service.dimension = 384
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_service,
    ):
        yield fake_service.encode_one


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_corpus(root: Path, n: int = 3) -> None:
    """Create *n* simple .md files under *root* named ``entry-N.md``."""
    for i in range(n):
        _write(root / f"entry-{i}.md", f"Body for entry {i} talking about Kubernetes.")


# ---------------------------------------------------------------------------
# ingest_directory -- first run, idempotency, body change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_directory_inserts_every_file_on_first_run(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """First run against a fresh tenant inserts every file; embedding called once per file."""
    _make_corpus(tmp_path, n=3)
    tenant_id = uuid.uuid4()
    service = KbService()

    result = await service.ingest_directory(tmp_path, tenant_id)

    assert result.inserted_count == 3
    assert result.updated_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 0
    assert result.errors == []
    # Embedding called once per file on the first-index path.
    assert stub_embedding.call_count == 3


@pytest.mark.asyncio
async def test_ingest_directory_is_idempotent_via_body_hash_skip(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """Second run against unchanged corpus skips every file; no embedding calls."""
    _make_corpus(tmp_path, n=3)
    tenant_id = uuid.uuid4()
    service = KbService()

    await service.ingest_directory(tmp_path, tenant_id)
    embed_count_after_first = stub_embedding.call_count

    result = await service.ingest_directory(tmp_path, tenant_id)

    assert result.inserted_count == 0
    assert result.updated_count == 0
    assert result.skipped_count == 3
    assert result.error_count == 0
    # No new embedding calls -- the body-hash short-circuit fired.
    assert stub_embedding.call_count == embed_count_after_first


@pytest.mark.asyncio
async def test_ingest_directory_detects_body_change_as_updated(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """A file whose body changed between runs counts as ``updated``."""
    _make_corpus(tmp_path, n=3)
    tenant_id = uuid.uuid4()
    service = KbService()

    await service.ingest_directory(tmp_path, tenant_id)

    # Change one file's body.
    (tmp_path / "entry-1.md").write_text(
        "Different body content for entry 1.",
        encoding="utf-8",
    )

    result = await service.ingest_directory(tmp_path, tenant_id)
    assert result.updated_count == 1
    assert result.skipped_count == 2
    assert result.inserted_count == 0


@pytest.mark.asyncio
async def test_ingest_directory_dry_run_classifies_without_writing(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """``dry_run=True`` returns counts but writes nothing to the documents table."""
    _make_corpus(tmp_path, n=2)
    tenant_id = uuid.uuid4()
    service = KbService()

    result = await service.ingest_directory(tmp_path, tenant_id, dry_run=True)
    assert result.inserted_count == 2
    assert result.skipped_count == 0

    # No rows written.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(Document).where(Document.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert rows == []
    # And no embedding compute happened.
    assert stub_embedding.call_count == 0


@pytest.mark.asyncio
async def test_ingest_directory_continues_past_bad_files(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """Mixed corpus: good + bad slug + binary masquerading as .md.

    The good file is ingested; the two bad files are counted in
    ``error_count`` with per-file messages in ``errors``. The run
    does not abort.
    """
    _write(tmp_path / "good-entry.md", "Good body.")
    _write(tmp_path / "BadCase.md", "Invalid slug body.")
    (tmp_path / "binary.md").write_bytes(b"\x00\x01\x02\xff\xfe")

    tenant_id = uuid.uuid4()
    service = KbService()
    result = await service.ingest_directory(tmp_path, tenant_id)

    assert result.inserted_count == 1
    assert result.error_count == 2
    assert any("BadCase" in e for e in result.errors)
    assert any("binary" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_does_not_leak_to_other_tenant(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """Tenant A ingests; tenant B's list/get sees nothing."""
    _make_corpus(tmp_path, n=2)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    service = KbService()

    await service.ingest_directory(tmp_path, tenant_a)

    a_entries = await service.list_entries(tenant_a)
    b_entries = await service.list_entries(tenant_b)
    assert len(a_entries) == 2
    assert b_entries == []

    # Cross-tenant get returns None even for an existing slug.
    found_for_a = await service.get_entry(tenant_a, "entry-0")
    found_for_b = await service.get_entry(tenant_b, "entry-0")
    assert found_for_a is not None
    assert found_for_b is None


# ---------------------------------------------------------------------------
# list_entries / get_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entries_returns_slug_sorted_entries(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """``list_entries`` returns sorted-by-slug; metadata enriched with source_path."""
    _write(tmp_path / "zebra.md", "Z.")
    _write(tmp_path / "alpha.md", "A.")
    _write(tmp_path / "mid.md", "M.")
    tenant_id = uuid.uuid4()
    service = KbService()

    await service.ingest_directory(tmp_path, tenant_id)
    entries = await service.list_entries(tenant_id)
    assert [e.slug for e in entries] == ["alpha", "mid", "zebra"]
    # source_path metadata was enriched.
    for entry in entries:
        assert "source_path" in entry.metadata


@pytest.mark.asyncio
async def test_list_entries_filter_pattern_narrows(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """``filter_pattern`` forwarded to SQL ``LIKE`` narrows the result set."""
    _write(tmp_path / "vault-jwt.md", "Vault jwt.")
    _write(tmp_path / "vault-kv.md", "Vault kv.")
    _write(tmp_path / "k8s-rbac.md", "K8s rbac.")
    tenant_id = uuid.uuid4()
    service = KbService()
    await service.ingest_directory(tmp_path, tenant_id)

    vault_only = await service.list_entries(tenant_id, filter_pattern="vault-%")
    assert sorted(e.slug for e in vault_only) == ["vault-jwt", "vault-kv"]


@pytest.mark.asyncio
async def test_list_entries_limit_zero_short_circuits(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """``limit=0`` skips the SQL round-trip; returns ``[]``."""
    _make_corpus(tmp_path, n=2)
    tenant_id = uuid.uuid4()
    service = KbService()
    await service.ingest_directory(tmp_path, tenant_id)
    assert await service.list_entries(tenant_id, limit=0) == []


@pytest.mark.asyncio
async def test_list_entries_negative_limit_raises(stub_embedding: AsyncMock) -> None:
    """Negative ``limit`` is operator misconfiguration; fail fast."""
    service = KbService()
    with pytest.raises(ValueError):
        await service.list_entries(uuid.uuid4(), limit=-1)


@pytest.mark.asyncio
async def test_get_entry_returns_full_body(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """``get_entry`` returns the full body; metadata; UUIDs."""
    _write(tmp_path / "deep-dive.md", "Long body about Kubernetes RBAC.")
    tenant_id = uuid.uuid4()
    service = KbService()
    await service.ingest_directory(tmp_path, tenant_id)

    entry = await service.get_entry(tenant_id, "deep-dive")
    assert entry is not None
    assert entry.body == "Long body about Kubernetes RBAC."
    assert entry.slug == "deep-dive"


@pytest.mark.asyncio
async def test_get_entry_returns_none_for_unknown_slug(
    stub_embedding: AsyncMock,
) -> None:
    """Unknown slug → ``None`` (not an exception)."""
    service = KbService()
    assert await service.get_entry(uuid.uuid4(), "does-not-exist") is None


# ---------------------------------------------------------------------------
# create_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_entry_writes_new_row(stub_embedding: AsyncMock) -> None:
    """``create_entry`` inserts a row; ``get_entry`` round-trips it."""
    tenant_id = uuid.uuid4()
    service = KbService()
    created = await service.create_entry(
        tenant_id=tenant_id,
        slug="net-policy",
        body="NetworkPolicy default-deny baseline.",
        metadata={"author": "ops"},
    )
    assert created.slug == "net-policy"
    assert created.metadata == {"author": "ops"}

    fetched = await service.get_entry(tenant_id, "net-policy")
    assert fetched is not None
    assert fetched.body == "NetworkPolicy default-deny baseline."


@pytest.mark.asyncio
async def test_create_entry_rejects_invalid_slug(stub_embedding: AsyncMock) -> None:
    """``create_entry`` calls :func:`validate_slug` before touching the substrate."""
    service = KbService()
    with pytest.raises(InvalidKbSlugError):
        await service.create_entry(
            tenant_id=uuid.uuid4(),
            slug="BadCase",
            body="Body.",
        )
    # The embedding pipeline was never invoked -- validation aborted first.
    assert stub_embedding.call_count == 0


# ---------------------------------------------------------------------------
# delete_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_removes_existing_row(
    tmp_path: Path,
    stub_embedding: AsyncMock,
) -> None:
    """``delete_entry`` removes; ``get_entry`` returns ``None`` afterwards; returns True."""
    _write(tmp_path / "doomed.md", "About to be deleted.")
    tenant_id = uuid.uuid4()
    service = KbService()
    await service.ingest_directory(tmp_path, tenant_id)

    removed = await service.delete_entry(tenant_id, "doomed")
    assert removed is True
    assert await service.get_entry(tenant_id, "doomed") is None


@pytest.mark.asyncio
async def test_delete_entry_returns_false_for_unknown_slug(
    stub_embedding: AsyncMock,
) -> None:
    """Deleting a slug that doesn't exist returns ``False``."""
    service = KbService()
    assert await service.delete_entry(uuid.uuid4(), "missing-slug") is False


# ---------------------------------------------------------------------------
# search_entries (mocked retrieve)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_entries_pins_kb_source_and_adapts_hits(
    stub_embedding: AsyncMock,
) -> None:
    """``search_entries`` calls retrieve with ``source='kb'`` and adapts the result shape."""
    tenant_id = uuid.uuid4()
    fake_hit = RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="kb",
        source_id="example-slug",
        kind="kb-entry",
        body="A" * 500,  # longer than the snippet width to verify truncation
        doc_metadata={"author": "ops"},
        fused_score=0.5,
        bm25_score=0.3,
        cosine_score=0.7,
        bm25_rank=1,
        cosine_rank=2,
    )

    captured: dict[str, object] = {}

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return [fake_hit]

    service = KbService()
    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        hits = await service.search_entries(tenant_id, "kubernetes ingress")

    # Pinned to KB source; no kind filter unless caller passes one.
    assert captured["source"] == "kb"
    assert captured["kind"] is None

    assert len(hits) == 1
    hit = hits[0]
    assert hit.slug == "example-slug"
    # Snippet truncated at the documented width with an ellipsis.
    assert len(hit.snippet) < 500
    assert hit.snippet.endswith("…")
    assert hit.bm25_score == 0.3
    assert hit.cosine_rank == 2


@pytest.mark.asyncio
async def test_search_entries_kind_filter_passthrough(
    stub_embedding: AsyncMock,
) -> None:
    """A ``kind`` filter in the ``filters`` dict reaches the retrieve call."""
    captured: dict[str, object] = {}

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return []

    service = KbService()
    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        await service.search_entries(
            uuid.uuid4(),
            "query",
            filters={"kind": "kb-entry"},
        )
    assert captured["kind"] == "kb-entry"


@pytest.mark.asyncio
async def test_search_entries_short_body_snippet_is_full_body(
    stub_embedding: AsyncMock,
) -> None:
    """When the body fits within the snippet width, snippet == body (no ellipsis)."""
    short = "Just a short body."
    fake_hit = RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        source="kb",
        source_id="short",
        kind="kb-entry",
        body=short,
        doc_metadata={},
        fused_score=0.1,
        bm25_score=None,
        cosine_score=0.1,
        bm25_rank=None,
        cosine_rank=1,
    )

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        return [fake_hit]

    service = KbService()
    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        hits = await service.search_entries(uuid.uuid4(), "q")
    assert hits[0].snippet == short
    assert not hits[0].snippet.endswith("…")
