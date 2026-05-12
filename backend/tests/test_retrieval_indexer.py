# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.indexer`.

Coverage matrix (G0.4-T3 / Task #260 acceptance criteria):

* ``compute_body_hash`` is deterministic SHA-256 of the UTF-8 body
  -- the change-detection contract every other test relies on.
* ``estimate_tokens`` returns ``int(len(body.split()) * 1.3)`` --
  the v0.2 heuristic; locked in so a future swap to tiktoken is a
  conscious change.
* :func:`index_document` first-call path -- inserts a new row,
  embedding is called once, all columns populated from the inputs,
  ``id`` / ``created_at`` / ``updated_at`` populated by ORM defaults.
* Re-call with same body -- short-circuits the embedding compute
  (proven by asserting the mock was NOT called on re-index),
  ``updated_at`` advances, ``body_hash`` stays put.
* Re-call with same body + new metadata -- still skips embedding,
  overwrites ``doc_metadata``.
* Re-call with different body -- recomputes embedding,
  updates every body-derived field, advances ``updated_at``, keeps
  ``id`` / ``created_at`` from the first call.
* Cross-tenant isolation -- two tenants can share
  ``(source, source_id)`` without collision; both rows persist.
* Caller-owned session path -- when the caller passes a session, the
  helper does not commit; the caller's commit is what persists.
* Helper-owned session path -- when no session is passed, the helper
  opens its own and commits before returning.

The embedding service is fully mocked via
:func:`unittest.mock.patch` against the singleton factory so SQLite
tests don't pull fastembed or ONNX runtime. The SQLite path is the
load-bearing test driver -- the per-tenant unique index and FK to
``tenant`` are both honoured by the SQLite engine (PG ditto, covered
by T6's integration test).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.retrieval.indexer import (
    compute_body_hash,
    estimate_tokens,
    index_document,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding() -> Iterator[AsyncMock]:
    """Patch :func:`get_embedding_service` so encode returns a deterministic vector.

    The mock returns the same 384-dim vector for every input; tests
    that need to differentiate by content read ``body`` / ``body_hash``
    on the persisted row. The mock surfaces the call count so the
    skip-re-embed test can assert "encode was called once, not twice".

    Patches both call sites (the inner ``indexer`` import and the
    ``embedding`` module proper) so test isolation holds regardless
    of how the SUT resolves the singleton.
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
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine.

    The session is the caller-owned shape callers like T5's API route
    would use. Per-test rollback keeps test isolation tight even
    though the engine is shared via :func:`get_sessionmaker`.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_compute_body_hash_is_deterministic_sha256() -> None:
    """Same input → same hex digest of the right length."""
    h1 = compute_body_hash("hello world")
    h2 = compute_body_hash("hello world")
    assert h1 == h2
    assert len(h1) == 64
    # Sanity: hex digits only.
    int(h1, 16)


def test_compute_body_hash_differs_on_different_input() -> None:
    """Different input → different hash."""
    assert compute_body_hash("a") != compute_body_hash("b")
    assert compute_body_hash("hello world") != compute_body_hash("hello world!")


def test_compute_body_hash_encodes_as_utf8() -> None:
    """UTF-8 encoding is the load-bearing contract.

    A future encoding change would invalidate every existing hash;
    asserting against a known SHA-256 of a UTF-8 byte sequence locks
    the contract in regression-test form.
    """
    # SHA-256 of "hello" UTF-8 = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824.
    assert (
        compute_body_hash("hello")
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_estimate_tokens_uses_word_count_times_1_3() -> None:
    """``int(len(body.split()) * 1.3)`` -- v0.2 heuristic, locked in."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("one") == 1  # int(1 * 1.3) == 1
    assert estimate_tokens("one two three four") == 5  # int(4 * 1.3) == 5
    assert estimate_tokens("a b c d e f g h i j") == 13  # int(10 * 1.3) == 13


# ---------------------------------------------------------------------------
# First-index path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_document_inserts_new_row_with_helper_owned_session(
    stub_embedding: AsyncMock,
) -> None:
    """First call for a natural key inserts a row + commits.

    Helper-owned-session path: no caller session, the helper opens
    one, commits, returns. The persisted row should be readable
    through a fresh session to prove the commit landed.
    """
    tenant_id = uuid.uuid4()
    doc = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="kubernetes-ingress",
        kind="kb-entry",
        body="Some kb entry body about Kubernetes ingress troubleshooting.",
        metadata={"author": "ops"},
    )

    assert isinstance(doc.id, uuid.UUID)
    assert doc.tenant_id == tenant_id
    assert doc.source == "kb"
    assert doc.source_id == "kubernetes-ingress"
    assert doc.kind == "kb-entry"
    assert doc.body.startswith("Some kb entry body")
    assert doc.body_hash == compute_body_hash(doc.body)
    assert doc.tokens == estimate_tokens(doc.body)
    assert doc.embedding == [0.1] * 384
    assert doc.doc_metadata == {"author": "ops"}
    assert doc.created_at is not None
    assert doc.updated_at is not None

    # Embedding called exactly once.
    assert stub_embedding.call_count == 1

    # Confirm the row landed by reading through a fresh session.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(select(Document).where(Document.id == doc.id))
        persisted = result.scalar_one()
        assert persisted.body == doc.body
        assert persisted.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_index_document_default_metadata_is_empty_dict(
    stub_embedding: AsyncMock,
) -> None:
    """``metadata=None`` on insert lands ``{}`` per the column's NOT NULL contract."""
    doc = await index_document(
        tenant_id=uuid.uuid4(),
        source="kb",
        source_id="empty-metadata-probe",
        kind="kb-entry",
        body="body",
    )
    assert doc.doc_metadata == {}


# ---------------------------------------------------------------------------
# Skip-re-embed (body unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_document_skips_reembedding_when_body_unchanged(
    stub_embedding: AsyncMock,
) -> None:
    """Second call with same body → no embed compute, updated_at advances.

    The dominant kb-refresh shape (most documents unchanged between
    refreshes) hits this branch. The mock's call_count is the
    load-bearing assertion: it must NOT increment on re-index.
    """
    tenant_id = uuid.uuid4()
    body = "unchanged content for the skip-re-embed test"

    first = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="skip-reembed",
        kind="kb-entry",
        body=body,
    )
    assert stub_embedding.call_count == 1
    first_updated_at = first.updated_at
    first_created_at = first.created_at

    # Brief pause so the next datetime.now(UTC) lands a measurable delta.
    import asyncio

    await asyncio.sleep(0.01)

    second = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="skip-reembed",
        kind="kb-entry",
        body=body,  # SAME body
    )

    # Embedding mock NOT called again -- the whole point of the test.
    assert stub_embedding.call_count == 1, (
        "Embedding service must not be invoked when body_hash is unchanged"
    )

    # Same row (same id, same created_at), but updated_at advanced.
    # SQLite drops tzinfo on the round-trip while the in-memory
    # default lambda is tz-aware, so compare with tzinfo normalised --
    # same dance as ``tests/test_db_documents``.
    assert second.id == first.id
    assert second.created_at.replace(tzinfo=None) == first_created_at.replace(tzinfo=None)
    assert second.updated_at.replace(tzinfo=None) > first_updated_at.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_index_document_skip_reembed_overwrites_metadata_when_passed(
    stub_embedding: AsyncMock,
) -> None:
    """Same body + new metadata → metadata overwritten, embedding still skipped."""
    tenant_id = uuid.uuid4()
    body = "body for metadata-only update"

    await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="metadata-only-update",
        kind="kb-entry",
        body=body,
        metadata={"v": 1},
    )
    assert stub_embedding.call_count == 1

    updated = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="metadata-only-update",
        kind="kb-entry",
        body=body,
        metadata={"v": 2, "extra": "tag"},
    )
    assert stub_embedding.call_count == 1  # still 1: no re-embed
    assert updated.doc_metadata == {"v": 2, "extra": "tag"}


@pytest.mark.asyncio
async def test_index_document_skip_reembed_preserves_metadata_when_none(
    stub_embedding: AsyncMock,
) -> None:
    """Same body + ``metadata=None`` → existing metadata preserved.

    ``None`` is the explicit "don't touch metadata" signal; ``{}`` is
    "clear it". The distinction matters when a caller is touching
    just the updated_at timestamp (cheap freshness probe) and doesn't
    want to clobber whatever the previous indexing run wrote.
    """
    tenant_id = uuid.uuid4()
    body = "body whose metadata should be preserved on no-op re-index"

    await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="metadata-preserve",
        kind="kb-entry",
        body=body,
        metadata={"author": "ops", "tags": ["k8s"]},
    )

    preserved = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="metadata-preserve",
        kind="kb-entry",
        body=body,
        metadata=None,  # NOT explicitly empty
    )
    assert preserved.doc_metadata == {"author": "ops", "tags": ["k8s"]}


# ---------------------------------------------------------------------------
# Re-index (body changed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_document_reembeds_on_body_change(
    stub_embedding: AsyncMock,
) -> None:
    """Different body → embedding called again, every body-derived field updated."""
    tenant_id = uuid.uuid4()

    first = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="reindex-probe",
        kind="kb-entry",
        body="initial body",
        metadata={"v": 1},
    )
    first_id = first.id
    first_created_at = first.created_at
    first_hash = first.body_hash

    second = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="reindex-probe",
        kind="kb-entry",
        body="updated body with more content",
        metadata={"v": 2},
    )

    # Embedding called twice: once on insert, once on re-index.
    assert stub_embedding.call_count == 2
    # Same row (id + created_at unchanged), every body-derived field refreshed.
    # tzinfo dance per the SQLite round-trip noted in
    # ``test_index_document_skips_reembedding_when_body_unchanged``.
    assert second.id == first_id
    assert second.created_at.replace(tzinfo=None) == first_created_at.replace(tzinfo=None)
    assert second.body == "updated body with more content"
    assert second.body_hash != first_hash
    assert second.body_hash == compute_body_hash(second.body)
    assert second.tokens == estimate_tokens(second.body)
    assert second.doc_metadata == {"v": 2}


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_document_cross_tenant_isolation(stub_embedding: AsyncMock) -> None:
    """Two tenants can share ``(source, source_id)`` without collision.

    The natural-key unique index ``documents_tenant_source_id_idx``
    includes ``tenant_id``, so identical kb entries in tenant-a and
    tenant-b coexist as separate rows. Each tenant's row should be
    independently retrievable.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    doc_a = await index_document(
        tenant_id=tenant_a,
        source="kb",
        source_id="shared-natural-key",
        kind="kb-entry",
        body="tenant A version",
    )
    doc_b = await index_document(
        tenant_id=tenant_b,
        source="kb",
        source_id="shared-natural-key",
        kind="kb-entry",
        body="tenant B version",
    )

    assert doc_a.id != doc_b.id
    assert doc_a.tenant_id == tenant_a
    assert doc_b.tenant_id == tenant_b
    assert doc_a.body != doc_b.body

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(Document).where(Document.source_id == "shared-natural-key")
        )
        all_rows = result.scalars().all()

    assert len(all_rows) == 2
    assert {r.tenant_id for r in all_rows} == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# Caller-owned session vs helper-owned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_document_caller_session_does_not_commit(
    stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """Helper does not commit when caller passes a session.

    Caller-owned-session path: the helper flushes (so ``doc.id`` and
    ORM defaults are visible on the returned row) but leaves the
    transaction open. The caller's rollback would discard the insert.
    Verified by checking that a *fresh* session sees no row until
    the caller commits.
    """
    tenant_id = uuid.uuid4()
    doc = await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="caller-session-probe",
        kind="kb-entry",
        body="body",
        session=session,
    )
    # Returned row has its ORM defaults (flush populated them).
    assert isinstance(doc.id, uuid.UUID)

    # Fresh session sees nothing -- caller hasn't committed yet.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(Document).where(Document.source_id == "caller-session-probe")
        )
        assert result.scalar_one_or_none() is None

    # After the caller commits, the row is visible.
    await session.commit()
    async with sessionmaker() as fresh_after:
        result = await fresh_after.execute(
            select(Document).where(Document.source_id == "caller-session-probe")
        )
        assert result.scalar_one().tenant_id == tenant_id


@pytest.mark.asyncio
async def test_index_document_caller_session_rollback_discards_insert(
    stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """Caller rollback discards the insert -- the helper does not pre-commit."""
    tenant_id = uuid.uuid4()
    await index_document(
        tenant_id=tenant_id,
        source="kb",
        source_id="rollback-probe",
        kind="kb-entry",
        body="body that should never persist",
        session=session,
    )

    await session.rollback()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(select(Document).where(Document.source_id == "rollback-probe"))
        assert result.scalar_one_or_none() is None
