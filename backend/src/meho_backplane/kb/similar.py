# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Show similar knowledge entries before a new entry is written.

A create of a new slug first looks for entries that already say
something similar. When it finds some, nothing is written: the caller
gets the list and either updates one of those entries, or creates the
new entry anyway on purpose (``create_anyway``). A dry run gives the
same answer and never writes.

How an entry counts as similar:

1. Entries with the same ``product`` and ``version`` in the lesson header
   are searched first, and listed first.
2. Then all entries of the tenant (and the shared scope) are searched.

Both searches rank the entry passages by meaning (cosine score of the
embeddings). Only an entry whose best passage scores at least
``KB_SIMILAR_MIN_SCORE`` (setting ``kb_similar_min_score``) is kept. At
most :data:`SIMILAR_LIMIT` entries are listed.

The check is advice at write time, not a lock: a similar entry that
another writer saves after the check is not caught. The weekly upkeep
merges such duplicates later.

The surfaces call :func:`check_new_entry` before
:meth:`~meho_backplane.kb.service.KbService.create_entry`. Directory
ingest and the import job do not call it.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.revisions import current_revision_id
from meho_backplane.kb.schemas import (
    KB_SOURCE,
    META_CREATED_BY_SUB,
    validate_slug,
)
from meho_backplane.kb.service import _hit_slug, add_shared_hits
from meho_backplane.kb.shared import refuse_shared_slug, visible_to
from meho_backplane.lesson_header import normalise_lesson_header
from meho_backplane.retrieval.retriever import CANDIDATE_LIMIT, RetrievalHit, retrieve
from meho_backplane.settings import get_settings

__all__ = [
    "SIMILAR_LIMIT",
    "KbSimilarCheck",
    "KbSimilarEntry",
    "check_new_entry",
    "find_similar_entries",
]

#: Most similar entries one answer lists.
SIMILAR_LIMIT: Final[int] = 5

#: The search text is the summary and the start of the body. The
#: embedding model reads about 512 tokens, so more text adds nothing.
_QUERY_CHARS: Final[int] = 2000

#: Longest summary and snippet in the answer, so the answer stays small.
_TEXT_CHARS: Final[int] = 200

_NEXT_STEP_SIMILAR: Final[str] = (
    "Nothing was written. Update one of the similar entries instead: read it, "
    "then write it again under its own slug with the merged text (its "
    "revision_id names the revision you read). Or create the new entry on "
    "purpose with create_anyway set to true."
)
_NEXT_STEP_WOULD_CREATE: Final[str] = (
    "Dry run: nothing was written. No similar entry was found, so the create would go through."
)
_NEXT_STEP_WOULD_CREATE_ANYWAY: Final[str] = (
    "Dry run: nothing was written. With create_anyway the new entry would be "
    "written even though similar entries exist."
)
_NEXT_STEP_WOULD_UPDATE: Final[str] = (
    "Dry run: nothing was written. An entry with this slug exists, so the "
    "write would update it. The similar-entries check runs only for a new slug."
)


class KbSimilarEntry(BaseModel):
    """One existing entry that is similar to the entry about to be created."""

    slug: str
    entry_id: uuid.UUID
    #: The revision the entry is at now. Name it when you update the entry.
    revision_id: uuid.UUID | None
    summary: str | None
    product: str | None
    version: str | None
    status: str | None
    snippet: str
    #: Cosine score of the best matching passage, 0 to 1.
    score: float
    #: ``same_product_version``: the lesson header names the same product
    #: and version. ``close_meaning``: only the text is close.
    match: Literal["same_product_version", "close_meaning"]
    #: Whether the caller may update this entry (the owner rule).
    may_change: bool


class KbSimilarCheck(BaseModel):
    """The answer of a create that wrote nothing.

    Given when similar entries were found (``reason="similar_entries"``)
    or the caller asked for a dry run (``reason="dry_run"``).
    """

    slug: str
    written: Literal[False] = False
    reason: Literal["similar_entries", "dry_run"]
    #: Whether the same call without ``dry_run`` would write the entry.
    would_write: bool
    #: Whether an entry with this slug exists (then the write is an update).
    slug_exists: bool
    similar: list[KbSimilarEntry]
    min_score: float
    next_step: str


async def check_new_entry(
    tenant_id: uuid.UUID,
    slug: str,
    body: str,
    metadata: dict[str, Any] | None,
    *,
    actor_sub: str | None,
    may_change_any: bool,
    create_anyway: bool = False,
    dry_run: bool = False,
) -> KbSimilarCheck | None:
    """Return the answer to give instead of writing, or ``None`` to write.

    ``None`` means: go on with the create. That is the case for an update
    of an existing slug, a new slug with nothing similar, and
    *create_anyway*. A dry run always returns an answer.

    Checks *slug*, the lesson header in *metadata* and the shared scope
    first, with the same errors as the create (``InvalidKbSlugError``,
    ``LessonHeaderError``, ``KbSharedSlugError``).
    *may_change_any* and *actor_sub* decide ``may_change`` of each listed
    entry (see :func:`find_similar_entries`).
    """
    validate_slug(slug)
    header = (
        normalise_lesson_header(metadata, allowed_reserved=frozenset({"provenance"}))
        if metadata
        else {}
    )
    await refuse_shared_slug(tenant_id, slug)
    if await _slug_exists(tenant_id, slug):
        if not dry_run:
            return None
        return _answer(slug, "dry_run", True, True, [], _NEXT_STEP_WOULD_UPDATE)
    if create_anyway and not dry_run:
        return None

    similar = await find_similar_entries(
        tenant_id,
        body,
        header,
        actor_sub=actor_sub,
        may_change_any=may_change_any,
    )
    if dry_run:
        would_write = create_anyway or not similar
        next_step = (
            _NEXT_STEP_SIMILAR
            if not would_write
            else _NEXT_STEP_WOULD_CREATE_ANYWAY
            if similar
            else _NEXT_STEP_WOULD_CREATE
        )
        return _answer(slug, "dry_run", would_write, False, similar, next_step)
    if similar:
        return _answer(slug, "similar_entries", False, False, similar, _NEXT_STEP_SIMILAR)
    return None


async def find_similar_entries(
    tenant_id: uuid.UUID,
    body: str,
    header: dict[str, Any],
    *,
    actor_sub: str | None,
    may_change_any: bool,
) -> list[KbSimilarEntry]:
    """Return up to :data:`SIMILAR_LIMIT` entries similar to *body*, best first.

    Entries with the same ``product`` and ``version`` as *header* come
    first, then the others; inside each group the higher score comes
    first. An entry the caller may change is one of its own tenant, and
    either *may_change_any* is set or *actor_sub* created it.
    """
    min_score = get_settings().kb_similar_min_score
    query = _query_text(body, header)
    product, version = header.get("product"), header.get("version")
    same: dict[str, float] = {}
    if isinstance(product, str) and isinstance(version, str):
        same = _best_scores(
            await _search(tenant_id, query, {"product": product, "version": version}),
            min_score,
        )
    close = _best_scores(await _search(tenant_id, query, None), min_score)
    ranked = sorted(same.items(), key=lambda item: -item[1]) + sorted(
        ((slug, score) for slug, score in close.items() if slug not in same),
        key=lambda item: -item[1],
    )
    ranked = ranked[:SIMILAR_LIMIT]
    if not ranked:
        return []
    heads = await _heads(tenant_id, [slug for slug, _ in ranked])
    result: list[KbSimilarEntry] = []
    for slug, score in ranked:
        head = heads.get(slug)
        if head is None:
            # Deleted between the search and this read.
            continue
        doc, revision_id = head
        result.append(
            _similar_entry(
                doc,
                revision_id,
                score=score,
                same_product_version=slug in same,
                may_change=doc.tenant_id == tenant_id
                and (may_change_any or _created_by(doc) == actor_sub),
            )
        )
    return result


async def _search(
    tenant_id: uuid.UUID, query: str, metadata_filters: dict[str, Any] | None
) -> list[RetrievalHit]:
    """Rank the kb passages of the tenant and of the shared scope against *query*."""
    own = await retrieve(
        tenant_id=tenant_id,
        query=query,
        source=KB_SOURCE,
        limit=CANDIDATE_LIMIT,
        metadata_filters=metadata_filters,
    )
    return await add_shared_hits(
        own,
        tenant_id=tenant_id,
        query=query,
        kind=None,
        limit=CANDIDATE_LIMIT,
        metadata_filters=metadata_filters,
    )


def _best_scores(hits: list[RetrievalHit], min_score: float) -> dict[str, float]:
    """Return the best cosine score per entry slug, for scores of at least *min_score*."""
    best: dict[str, float] = {}
    for hit in hits:
        score = hit.cosine_score
        if score is None or score < min_score:
            continue
        slug = _hit_slug(hit)
        if score > best.get(slug, -1.0):
            best[slug] = score
    return best


async def _slug_exists(tenant_id: uuid.UUID, slug: str) -> bool:
    """Say whether the tenant itself has an entry with *slug*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        found = (
            await session.execute(
                select(Document.id)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                    Document.source_id == slug,
                )
                .limit(1)
            )
        ).first()
    return found is not None


async def _heads(
    tenant_id: uuid.UUID, slugs: list[str]
) -> dict[str, tuple[Document, uuid.UUID | None]]:
    """Read the head row and current revision id of each slug the tenant can see.

    The tenant's own entry wins over a shared one with the same slug.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Document, current_revision_id()).where(
                    visible_to(tenant_id),
                    Document.source == KB_SOURCE,
                    Document.source_id.in_(slugs),
                )
            )
        ).all()
    heads: dict[str, tuple[Document, uuid.UUID | None]] = {}
    for doc, revision_id in rows:
        if doc.source_id not in heads or doc.tenant_id == tenant_id:
            heads[doc.source_id] = (doc, revision_id)
    return heads


def _similar_entry(
    doc: Document,
    revision_id: uuid.UUID | None,
    *,
    score: float,
    same_product_version: bool,
    may_change: bool,
) -> KbSimilarEntry:
    metadata = doc.doc_metadata or {}
    return KbSimilarEntry(
        slug=doc.source_id,
        entry_id=doc.id,
        revision_id=revision_id,
        summary=_short(metadata.get("summary")),
        product=_short(metadata.get("product")),
        version=_short(metadata.get("version")),
        status=_short(metadata.get("status")),
        snippet=_short(doc.body) or "",
        score=round(score, 4),
        match="same_product_version" if same_product_version else "close_meaning",
        may_change=may_change,
    )


def _created_by(doc: Document) -> str | None:
    creator = (doc.doc_metadata or {}).get(META_CREATED_BY_SUB)
    return creator if isinstance(creator, str) else None


def _short(value: object) -> str | None:
    """Return *value* as text of at most :data:`_TEXT_CHARS` characters, or ``None``."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _TEXT_CHARS else text[:_TEXT_CHARS] + "…"


def _query_text(body: str, header: dict[str, Any]) -> str:
    summary = header.get("summary")
    text = f"{summary}\n\n{body}" if isinstance(summary, str) and summary else body
    return text[:_QUERY_CHARS]


def _answer(
    slug: str,
    reason: Literal["similar_entries", "dry_run"],
    would_write: bool,
    slug_exists: bool,
    similar: list[KbSimilarEntry],
    next_step: str,
) -> KbSimilarCheck:
    return KbSimilarCheck(
        slug=slug,
        reason=reason,
        would_write=would_write,
        slug_exists=slug_exists,
        similar=similar,
        min_score=get_settings().kb_similar_min_score,
        next_step=next_step,
    )
