# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``index_document`` -- the canonical write path for retrievable documents.

G0.4-T3 (#260) of Initiative #225. The shared upsert helper G4 (#215,
kb ingestion) and G5 (#216, memory writes) both call to put a row into
the ``documents`` table; both paths consume the same hash-dedup +
embed + commit sequence, so it lives here once rather than being
re-derived per consumer.

Algorithm
---------

For each ``(tenant_id, source, source_id)`` natural key:

1. Look up the existing row (if any).
2. Compute the SHA-256 hex digest of the incoming body.
3. If the row exists and its ``body_hash`` matches the incoming body:
   short-circuit. Touch ``updated_at`` (and ``doc_metadata`` if the
   caller passed a new dict), commit, return. **No embed call.** This
   is the cost optimisation that makes ``meho kb refresh`` against
   an unchanged corpus essentially free: the embedding compute
   (~10-50 ms per text on CPU) is skipped for every document whose
   body hasn't changed.
4. Otherwise compute the embedding via
   :func:`~meho_backplane.retrieval.embedding.get_embedding_service`,
   then either update the existing row in-place (re-index path) or
   insert a fresh row (first-index path). Commit and return.

Tenant-scoping is the caller's responsibility -- the function takes
``tenant_id`` as an explicit parameter rather than reading it off a
contextvar. T5's ``/api/v1/retrieve`` route extracts it from
``Operator.tenant_id`` (which the JWT verifier bound); G4 / G5
ingestion paths extract it the same way from their own auth chains.
This makes the helper trivially testable in isolation and keeps the
tenant boundary auditable at the call site rather than buried in a
contextvar resolver.

Session handling
----------------

The helper accepts an optional ``session`` argument. When provided,
the caller owns the transaction boundary (commit / rollback); useful
when ``index_document`` is one step in a larger ingestion (kb refresh
that batches dozens of documents) and the caller wants the whole
batch atomic. When ``None``, the helper opens its own
:func:`~meho_backplane.db.engine.get_sessionmaker` session, commits,
and closes -- the convenient one-off-call shape.

Prepared batches
----------------

:func:`prepare_index_batch` is the second write path, for a caller
that writes several rows as one unit (the kb passage index, #3761).
It computes every embedding first, with no transaction open, and
returns a :class:`PreparedIndexBatch`. The caller then opens its short
transaction, calls :meth:`PreparedIndexBatch.apply` with its own
session, and commits. The slow model step never holds a row lock, and
when it fails nothing has been written. A batch is used once: a second
:meth:`~PreparedIndexBatch.apply` raises, so a retry prepares again.

Out of scope (deferred per Initiative body)
-------------------------------------------

* Background re-indexing on model change -- operators run a one-off
  re-index script when swapping models.
* Token estimation via tiktoken -- v0.2 uses the rough heuristic
  ``len(body.split()) * 1.3``. The ``tokens`` column is informational
  (budget-tracking for future agent flows), not load-bearing for
  retrieval.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.retrieval.embedding import PassageTokenizer, get_embedding_service

__all__ = [
    "IndexItem",
    "PreparedIndexBatch",
    "compute_body_hash",
    "estimate_tokens",
    "get_passage_tokenizer",
    "index_document",
    "prepare_index_batch",
]


def compute_body_hash(body: str) -> str:
    """SHA-256 hex digest of *body* (UTF-8 encoded).

    The hash is the change-detection probe ``index_document`` uses to
    decide whether to skip the embedding compute on re-indexing. Pure
    function; same input always returns the same hex digest. UTF-8
    encoding is the load-bearing contract -- changing the encoding
    invalidates every existing hash in the corpus and forces a
    re-embed-everything migration.

    SHA-256 is overkill for a change-detection probe (a 32-bit CRC
    would functionally suffice), but the wider digest gives near-zero
    collision risk across a tenant's lifetime, is stdlib-available on
    every supported Python, and lets the same value double as a
    coarse content-identity token if T4 / T5 ever want to surface it.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def estimate_tokens(body: str) -> int:
    """Rough word-count token estimate -- multiplier 1.3 for English.

    The ``tokens`` column is informational for v0.2 (G4 / G5 budget
    tracking, future agent-grounding flows that need to cap context
    size). A real estimate via :mod:`tiktoken` would add a fat
    dependency for a non-load-bearing signal; the v0.2 heuristic is
    accurate to within ~15% on English text -- good enough for budget
    decisions that are themselves order-of-magnitude.

    The 1.3 multiplier corresponds to the OpenAI BPE tokeniser's
    average word-to-token ratio on English; non-English bodies will
    under-count, which is the acceptable failure mode (budget-side
    over-counting risks rejecting valid requests).
    """
    return int(len(body.split()) * 1.3)


async def index_document(
    tenant_id: uuid.UUID,
    source: str,
    source_id: str,
    kind: str,
    body: str,
    metadata: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> Document:
    """Index (or re-index) one document for retrieval. Skip re-embed on unchanged body.

    Parameters
    ----------
    tenant_id
        The owning tenant's UUID. Caller-supplied; every document is
        owned by exactly one tenant and tenant-scoped queries are the
        only retrieval path.
    source
        Origin namespace -- one of ``"kb"`` / ``"memory"`` /
        ``"docs-sidecar"`` / future. Forms part of the natural-key
        upsert target ``(tenant_id, source, source_id)``.
    source_id
        The per-source natural-key identifier (kb slug, memory file
        path, etc.). Stored as text so consumers keep their own
        identifier conventions.
    kind
        Per-source classification (``"kb-entry"``, ``"memory-user"``,
        future). Enables retrieval filters that narrow within a
        source.
    body
        The document text -- what BM25 searches, what the embedding is
        computed from. Stored as-is; no chunking in v0.2.
    metadata
        Optional JSON-serialisable dict written to the
        ``doc_metadata`` column. ``None`` keeps the existing row's
        metadata on re-index (skip-re-embed path); ``{}`` explicitly
        clears it.
    session
        Optional caller-owned :class:`AsyncSession`. When provided the
        helper does **not** commit -- the caller controls transaction
        boundaries (useful for batch ingestion). When ``None`` the
        helper opens its own session, commits, and closes.

    Returns
    -------
    Document
        The persisted (or freshly-inserted) :class:`Document` row.
        The returned instance has every field populated including the
        ORM-side defaults (``id``, ``created_at``, ``updated_at``)
        that fire on first insert.

    Behavioural contract
    --------------------

    * **First call** for a given natural key -- inserts a new row,
      computes the embedding, populates ``tokens``, returns the row.
    * **Re-call with the same body** (``body_hash`` matches) -- skips
      the embedding compute, advances ``updated_at``, optionally
      overwrites ``doc_metadata``, returns the existing row.
    * **Re-call with a different body** -- recomputes the embedding,
      updates ``body`` / ``body_hash`` / ``kind`` / ``embedding`` /
      ``tokens`` / ``doc_metadata`` / ``updated_at``, returns the row.
    * **Cross-tenant isolation** -- the natural key includes
      ``tenant_id``, so two tenants can share ``(source, source_id)``
      without collision. The unique composite index
      ``documents_tenant_source_id_idx`` (migration ``0003``) enforces
      this at the DB layer.
    """
    if session is not None:
        return await _index_in_session(
            session,
            tenant_id,
            source,
            source_id,
            kind,
            body,
            metadata,
            commit=False,
        )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as owned_session:
        return await _index_in_session(
            owned_session,
            tenant_id,
            source,
            source_id,
            kind,
            body,
            metadata,
            commit=True,
        )


async def _index_in_session(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source: str,
    source_id: str,
    kind: str,
    body: str,
    metadata: dict[str, Any] | None,
    *,
    commit: bool,
) -> Document:
    """Inner implementation -- runs the upsert logic against *session*.

    Split out so the public :func:`index_document` can branch on
    caller-owned-vs-helper-owned session without duplicating the
    upsert path. The ``commit`` flag controls whether to issue the
    final commit: when the caller passes a session they own the
    transaction lifecycle, so the helper just flushes and returns;
    when the helper opens its own session it commits explicitly.

    A single :func:`session.flush` before each return path makes the
    ORM-side defaults (``Document.id`` from ``uuid.uuid4``,
    ``created_at`` from ``datetime.now(UTC)``) visible on the
    returned instance even when the caller defers the commit. Without
    the flush, a caller that reads ``doc.id`` immediately after
    receiving the returned row would get ``None`` on the insert path.
    """
    log = structlog.get_logger()
    now = datetime.now(UTC)
    new_hash = compute_body_hash(body)

    result = await session.execute(
        select(Document).where(
            Document.tenant_id == tenant_id,
            Document.source == source,
            Document.source_id == source_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None and existing.body_hash == new_hash:
        # Skip-re-embed path: body unchanged, just touch the timestamp
        # and optionally overwrite metadata. The dominant kb-refresh
        # shape (most documents unchanged) hits this branch.
        existing.updated_at = now
        if metadata is not None:
            existing.doc_metadata = metadata
        await session.flush()
        if commit:
            await session.commit()
        log.info(
            "document_indexed",
            action="skip_reembed",
            tenant_id=str(tenant_id),
            source=source,
            source_id=source_id,
        )
        return existing

    # Either no existing row OR body changed -- both paths embed.
    embedding = await get_embedding_service().encode_one(body)

    # Re-index path: the row exists but the body changed; ``id`` and
    # ``created_at`` stay the original first-index values. First-index
    # path: a brand-new row. Both then get every body-derived field.
    action = "reindex" if existing is not None else "insert"
    doc = existing
    if doc is None:
        doc = Document(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source=source,
            source_id=source_id,
            created_at=now,
        )
        session.add(doc)
    _assign(
        doc,
        kind=kind,
        body=body,
        embedding=embedding,
        tokens=estimate_tokens(body),
        metadata=metadata if metadata is not None else {},
        now=now,
    )
    await session.flush()
    if commit:
        await session.commit()
    log.info(
        "document_indexed",
        action=action,
        tenant_id=str(tenant_id),
        source=source,
        source_id=source_id,
    )
    return doc


def _assign(
    doc: Document,
    *,
    kind: str,
    body: str,
    embedding: list[float],
    tokens: int,
    metadata: dict[str, Any],
    now: datetime,
) -> None:
    """Write every body-derived field of *doc* (the re-index update)."""
    doc.body = body
    doc.body_hash = compute_body_hash(body)
    doc.kind = kind
    doc.embedding = embedding
    doc.tokens = tokens
    doc.doc_metadata = metadata
    doc.updated_at = now


@dataclass(frozen=True)
class IndexItem:
    """One row for :func:`prepare_index_batch`.

    ``embed_text`` is the text the embedding is made from; ``None``
    means ``body``. ``tokens`` is the stored token count; ``None``
    means :func:`estimate_tokens` of ``body``. ``document_id`` is the
    id a new row gets; ``None`` means a fresh UUID. An existing row
    with the same ``(tenant_id, source, source_id)`` keeps its id.
    """

    source: str
    source_id: str
    kind: str
    body: str
    metadata: dict[str, Any]
    embed_text: str | None = None
    tokens: int | None = None
    document_id: uuid.UUID | None = None


class PreparedIndexBatch:
    """Rows whose embeddings are already computed, ready to write once.

    Build it with :func:`prepare_index_batch`. :meth:`apply` writes the
    rows into a session the caller owns and does not commit.
    """

    def __init__(self, tenant_id: uuid.UUID, rows: list[tuple[IndexItem, list[float]]]) -> None:
        self._tenant_id = tenant_id
        self._rows = rows
        self._applied = False

    def __len__(self) -> int:
        return len(self._rows)

    async def apply(self, session: AsyncSession) -> list[Document]:
        """Insert or update every row in *session*; return them in batch order.

        Does not commit: the caller's transaction decides. Raises
        :class:`RuntimeError` when the batch was already applied, even
        if that first try failed -- prepare a new batch to retry.
        """
        if self._applied:
            raise RuntimeError("prepared index batch was already applied; prepare a new one")
        self._applied = True
        now = datetime.now(UTC)
        existing = await self._existing_rows(session)
        docs: list[Document] = []
        for item, embedding in self._rows:
            tokens = item.tokens if item.tokens is not None else estimate_tokens(item.body)
            doc = existing.get((item.source, item.source_id))
            if doc is None:
                doc = Document(
                    id=item.document_id or uuid.uuid4(),
                    tenant_id=self._tenant_id,
                    source=item.source,
                    source_id=item.source_id,
                    created_at=now,
                )
                session.add(doc)
            _assign(
                doc,
                kind=item.kind,
                body=item.body,
                embedding=embedding,
                tokens=tokens,
                metadata=item.metadata,
                now=now,
            )
            docs.append(doc)
        await session.flush()
        structlog.get_logger().info(
            "document_batch_indexed",
            tenant_id=str(self._tenant_id),
            rows=len(docs),
        )
        return docs

    async def _existing_rows(self, session: AsyncSession) -> dict[tuple[str, str], Document]:
        """Load the rows this batch overwrites, one query per source."""
        ids_by_source: dict[str, list[str]] = {}
        for item, _embedding in self._rows:
            ids_by_source.setdefault(item.source, []).append(item.source_id)
        found: dict[tuple[str, str], Document] = {}
        for source, source_ids in ids_by_source.items():
            result = await session.execute(
                select(Document).where(
                    Document.tenant_id == self._tenant_id,
                    Document.source == source,
                    Document.source_id.in_(source_ids),
                )
            )
            for doc in result.scalars():
                found[(doc.source, doc.source_id)] = doc
        return found


async def get_passage_tokenizer() -> PassageTokenizer:
    """Return the token counter of the model :func:`prepare_index_batch` embeds with.

    A caller that cuts text to fit the model window counts with this,
    so the count and the embeddings always come from the same model.
    """
    return await get_embedding_service().passage_tokenizer()


async def prepare_index_batch(
    tenant_id: uuid.UUID,
    items: Sequence[IndexItem],
) -> PreparedIndexBatch:
    """Compute the embeddings for *items* in one model call; write nothing.

    Call it before opening the transaction that applies the batch. When
    the model call fails, the error propagates and no row was touched.
    """
    texts = [item.body if item.embed_text is None else item.embed_text for item in items]
    vectors = await get_embedding_service().encode(texts) if texts else []
    if len(vectors) != len(items):
        raise RuntimeError(
            f"embedding model returned {len(vectors)} vectors for {len(items)} texts"
        )
    return PreparedIndexBatch(tenant_id, list(zip(items, vectors, strict=True)))
