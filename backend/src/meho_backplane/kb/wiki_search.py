# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search the tenant's team wiki next to the knowledge store (#5976).

A tenant may keep its lessons as pages in a Git wiki. A knowledge index
loads the wiki, and MEHO reaches the index as a docs collection. The tenant
setting ``tenant.knowledge_wiki_collection`` names that collection. When it
is set, ``search_knowledge`` (MCP) and ``meho kb search`` (``POST
/api/v1/retrieve`` with ``source="kb"``) also search it through
:func:`search_wiki`. When it is empty, nothing here runs and both searches
answer as before.

The wiki search uses the same parts as ``search_docs``: the entitlement
and readiness check
(:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`)
and the docs search service
(:func:`~meho_backplane.docs_search.search_docs`). It binds
``audit_collection`` (the key ``search_docs`` binds) and
``audit_wiki_status`` / ``audit_wiki_hit_count``, so the audit row of the
search names the collection and what happened.

The wiki is never dropped in silence. When it cannot be searched (no docs
add-on, no entitlement, an unknown or not-ready collection, the index does
not answer, or the search uses filters the wiki cannot apply), the store's
hits still come back and the answer says why the wiki was not searched.

``add_to_knowledge`` keeps storing every lesson in MEHO. On such a tenant
its answer also carries :func:`wiki_note_for`: the collection, and whether
the export (:mod:`meho_backplane.kb.page_export`) sends the lesson to the
wiki as a page for review, or why not yet.

A wiki page that the store also holds as an imported copy (provenance
``source_kind="import"`` with the page path) is shown once: as the store
hit, which has the slug for a full read and is what the recall before an
operation uses. The import refreshes the copy after every merge in the wiki.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import unquote, urlsplit

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.docs_search import (
    CollectionDisabledError,
    CollectionForbiddenError,
    CollectionNotReadyError,
    DocsChunk,
    UnknownCollectionError,
    build_docs_scope,
    collection_capability_key,
    resolve_entitled_ready_collection,
    search_docs,
)
from meho_backplane.kb.page_export import KbExportPage, KbExportRemoval, KbExportSkip, plan_entry
from meho_backplane.kb.provenance import read_provenance

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.kb.schemas import KbEntry

__all__ = [
    "DOCS_ADDON_CAPABILITY",
    "KbWikiSearch",
    "WikiSkipReason",
    "search_wiki",
    "wiki_collection_of",
    "wiki_note_for",
]

_log = structlog.get_logger(__name__)

#: The capability of the docs add-on, the same key that gates ``search_docs``.
DOCS_ADDON_CAPABILITY: Final[str] = "meho-docs"

WikiSkipReason = Literal[
    "docs_addon_off",
    "not_entitled",
    "unknown_collection",
    "collection_not_ready",
    "index_unavailable",
    "filtered",
]

_SEARCHED: Final[str] = "searched"


class KbWikiSearch(BaseModel):
    """What the wiki part of one knowledge search did.

    ``searched`` is false when the wiki was not searched; then ``reason``
    and ``message`` say why and ``hits`` is empty. ``hits`` are the wiki's
    hits, best first, each with ``collection`` set and its ``source_url``
    (the source link the index sent). ``duplicate_count`` counts the wiki
    hits left out because the store's hits already hold their imported copy.
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    searched: bool
    reason: WikiSkipReason | None = None
    message: str | None = None
    hits: list[DocsChunk] = Field(default_factory=list)
    duplicate_count: int = 0


async def wiki_collection_of(tenant_id: uuid.UUID) -> str | None:
    """Return the docs collection key where *tenant_id* keeps its lessons, or ``None``."""
    async with get_sessionmaker()() as session:
        value = await session.scalar(
            select(Tenant.knowledge_wiki_collection).where(Tenant.id == tenant_id)
        )
    return value or None


async def search_wiki(
    operator: Operator,
    query: str,
    *,
    limit: int,
    stored_metadata: Iterable[Mapping[str, object]],
    filtered: bool,
) -> KbWikiSearch | None:
    """Search the caller's wiki collection; ``None`` when the tenant has none.

    *stored_metadata* is the metadata of the store's hits in the same
    answer, used to leave out wiki pages the store holds as imported copies.
    *filtered* is true when the search uses filters, a time window or the
    shared scope: the wiki cannot apply them, so it is not searched.
    """
    key = await wiki_collection_of(operator.tenant_id)
    if key is None:
        return None
    if DOCS_ADDON_CAPABILITY not in operator.capabilities:
        return _not_searched(key, "docs_addon_off")
    if filtered:
        return _not_searched(key, "filtered")
    structlog.contextvars.bind_contextvars(audit_collection=key)
    try:
        async with get_sessionmaker()() as session:
            collection = await resolve_entitled_ready_collection(session, operator, key)
        result = await search_docs(
            operator, query, scope=build_docs_scope(key), collection=collection, limit=limit
        )
    except CollectionForbiddenError:
        return _not_searched(key, "not_entitled")
    except UnknownCollectionError:
        return _not_searched(key, "unknown_collection")
    except (CollectionNotReadyError, CollectionDisabledError):
        return _not_searched(key, "collection_not_ready")
    except CorpusUnavailable as exc:
        _log.warning("kb_wiki_search_unavailable", collection_key=key, error=str(exc))
        return _not_searched(key, "index_unavailable")
    imported = _imported_paths(stored_metadata)
    hits = [chunk.model_copy(update={"collection": key}) for chunk in result.chunks]
    kept = [chunk for chunk in hits if not _is_imported_copy(chunk, imported)]
    structlog.contextvars.bind_contextvars(
        audit_wiki_status=_SEARCHED, audit_wiki_hit_count=len(kept)
    )
    return KbWikiSearch(
        collection=key,
        searched=True,
        hits=kept,
        duplicate_count=len(hits) - len(kept),
    )


async def wiki_note_for(entry: KbEntry) -> dict[str, str] | None:
    """Return the wiki part of an ``add_to_knowledge`` answer; ``None`` without a wiki.

    ``export`` is what the export does with the stored entry: ``page`` (it
    goes to the wiki as a page for review), ``remove``, ``skip`` (not yet;
    ``message`` says what to fix) or ``none`` (the lesson stays in MEHO).
    """
    key = await wiki_collection_of(entry.tenant_id)
    if key is None:
        return None
    lead = f"Stored. This tenant keeps its lessons in the wiki behind collection `{key}`."
    planned = plan_entry(entry.slug, entry.body, dict(entry.metadata))
    if isinstance(planned, KbExportPage):
        status = entry.metadata.get("status")
        export, tail = "page", f"This lesson will also appear there as a {status} page for review."
    elif isinstance(planned, KbExportRemoval):
        export, tail = "remove", "Its page there will be removed for review."
    elif isinstance(planned, KbExportSkip):
        export, tail = "skip", f"It does not go there yet: {planned.reason}."
    else:
        export, tail = (
            "none",
            "This lesson stays in MEHO: only lessons with `applies_to: "
            "product-and-version` and status `candidate` or `confirmed` go to the wiki.",
        )
    return {"collection": key, "export": export, "message": f"{lead} {tail}"}


def _not_searched(key: str, reason: WikiSkipReason) -> KbWikiSearch:
    structlog.contextvars.bind_contextvars(audit_wiki_status=reason)
    return KbWikiSearch(
        collection=key, searched=False, reason=reason, message=_message(key, reason)
    )


def _message(key: str, reason: WikiSkipReason) -> str:
    why = {
        "docs_addon_off": (
            f"the docs add-on is not on for you (capability `{DOCS_ADDON_CAPABILITY}`)"
        ),
        "not_entitled": f"you lack the capability `{collection_capability_key(key)}`",
        "unknown_collection": "there is no docs collection with this key",
        "collection_not_ready": "the docs collection is not ready or is turned off",
        "index_unavailable": "the knowledge index did not answer; try again later",
        "filtered": (
            "it cannot apply filters, a time window or scope `shared`; "
            "search again without them to include it"
        ),
    }[reason]
    return f"The team wiki (docs collection `{key}`) was not searched: {why}."


def _imported_paths(stored_metadata: Iterable[Mapping[str, object]]) -> set[str]:
    """Return the wiki page paths of the imported copies among the store's hits."""
    paths: set[str] = set()
    for metadata in stored_metadata:
        provenance = read_provenance(dict(metadata))
        if provenance.source_kind == "import" and provenance.relative_path:
            paths.add(provenance.relative_path.strip("/"))
    return paths


def _is_imported_copy(chunk: DocsChunk, imported: set[str]) -> bool:
    """Say whether the page of *chunk* is one of the *imported* page paths.

    The chunk's page is read from its source link (``source_url`` or
    ``upstream_url``, for example ``.../blob/<commit>/products/x/page.md``)
    or its ``document_id``. A path matches when it ends with a whole
    imported path.
    """
    if not imported:
        return False
    candidates = [chunk.document_id or ""]
    for link in (chunk.source_url, chunk.upstream_url):
        if link:
            candidates.append(unquote(urlsplit(link).path))
    for candidate in candidates:
        text = candidate.strip("/")
        if any(text == path or text.endswith(f"/{path}") for path in imported):
            return True
    return False
