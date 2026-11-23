# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search the tenant's wiki next to the knowledge store.

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

A wiki page that the store also holds as an imported copy is shown once:
as the store hit, which has the slug for a full read and is what the recall
before an operation uses. The import refreshes the copy after every merge in
the wiki. A wiki hit is only one part of a page, and the import does not
record which repository a page came from. So neither the hit's text nor its
path proves that the stored copy is the same page. A wiki hit is left out
only when all of these hold:

* the caller's own tenant owns the store hit (a shared hit of another
  tenant comes from that tenant's wiki);
* its provenance says ``source_kind="import"``;
* its page path is the whole path of the wiki page: the path after
  ``blob/<commit>/`` in the hit's source link, or the hit's
  ``document_id`` (a page with only the same file name in another folder
  is a different page);
* the hit's text is part of the stored entry's text;
* the whole wiki page, read through the same ``read_docs`` service with
  the hit's ``read_handle`` (mode ``page``), is the stored entry's whole
  text. A lesson header at the top of the page is not compared, because
  the import does not store it in the text. Runs of spaces and line
  breaks count as one space, and Unicode text is compared in one normal
  form (NFC).

When any of these cannot be shown, the wiki hit stays: the hit has no
``read_handle`` (the collection does not offer read), the read is refused
or fails, it returns no text, or the backend cut the page or has more of it
to read. Each page is read at most once per search. A page is named by
the hit's ``document_id`` and source links, so pages at the same path in
another repository or at another commit are each read and compared.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import unquote, urlsplit

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, Tenant
from meho_backplane.docs_search import (
    CollectionDisabledError,
    CollectionForbiddenError,
    CollectionNotReadyError,
    DocsChunk,
    DocsReadError,
    DocsScope,
    UnknownCollectionError,
    build_docs_scope,
    collection_capability_key,
    forwarded_scope,
    read_docs,
    resolve_backend,
    resolve_entitled_ready_collection,
    search_docs,
)
from meho_backplane.kb.file_walker import KbFileParseError, parse_front_matter
from meho_backplane.kb.page_export import KbExportPage, KbExportRemoval, KbExportSkip, plan_entry
from meho_backplane.kb.provenance import read_provenance
from meho_backplane.kb.schemas import KB_SOURCE
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.docs_collections import DocCollection
    from meho_backplane.docs_search import DocsScope
    from meho_backplane.kb.schemas import KbEntry

__all__ = [
    "DOCS_ADDON_CAPABILITY",
    "KbWikiSearch",
    "WikiExactFilters",
    "WikiSkipReason",
    "search_wiki",
    "wiki_collection_of",
    "wiki_exact_filters",
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


class WikiExactFilters(BaseModel):
    """The ``product`` and ``version`` a knowledge search sends to the wiki's index."""

    model_config = ConfigDict(frozen=True)

    product: str | None = None
    version: str | None = None


def wiki_exact_filters(
    filters: Mapping[str, object] | None,
    *,
    has_time_window: bool = False,
    shared_scope: bool = False,
    kind: str | None = None,
) -> WikiExactFilters | None:
    """Return the filters the wiki's index may apply, or ``None``.

    The same rule for ``search_knowledge`` and ``POST /api/v1/retrieve``.
    With ``KB_WIKI_FILTERED_SEARCH`` off the answer is always ``None``, so
    every filter skips the wiki as before. With it on, the answer is a value
    only when the search has no time window, is not in the shared scope, has
    no ``kind``, and its filters are only ``product`` and/or ``version``,
    each a string that is not blank. Anything else gives ``None``. A value is
    kept as the caller gave it, of any length and spaces included, so the
    index gets the same string the knowledge store filters by, and
    ``read_docs`` reads its hits with that same string (with the switch on,
    ``read_docs`` takes a value of any length).
    """
    if not get_settings().kb_wiki_filtered_search:
        return None
    if has_time_window or shared_scope or kind is not None or not filters:
        return None
    if not set(filters) <= {"product", "version"}:
        return None
    if not all(isinstance(value, str) and value.strip() for value in filters.values()):
        return None
    product, version = filters.get("product"), filters.get("version")
    return WikiExactFilters(
        product=product if isinstance(product, str) else None,
        version=version if isinstance(version, str) else None,
    )


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
    stored: Iterable[tuple[uuid.UUID | None, str, Mapping[str, object]]],
    filtered: bool,
    exact: WikiExactFilters | None = None,
) -> KbWikiSearch | None:
    """Search the caller's wiki collection; ``None`` when the tenant has none.

    *stored* holds the owning tenant, the slug and the metadata of each
    store hit in the same answer, used to leave out wiki pages the store
    holds as imported copies.
    *filtered* is true when the search uses filters, a time window or the
    shared scope: the wiki cannot apply them, so it is not searched.
    *exact* (from :func:`wiki_exact_filters`) names filters the wiki can
    apply. Then a filtered search still runs, but only when the collection
    sends them to its index as exact filters (``scope_filters``); else the
    wiki is not searched and the answer says the index cannot apply them.
    """
    key = await wiki_collection_of(operator.tenant_id)
    if key is None:
        return None
    if DOCS_ADDON_CAPABILITY not in operator.capabilities:
        return _not_searched(key, "docs_addon_off")
    if filtered and exact is None:
        return _not_searched(key, "filtered")
    structlog.contextvars.bind_contextvars(audit_collection=key)
    try:
        async with get_sessionmaker()() as session:
            collection = await resolve_entitled_ready_collection(session, operator, key)
        scope = build_docs_scope(key)
        if exact:
            # Not built by build_docs_scope: it strips, and the index must get
            # the same string the store filters by.
            scope = DocsScope(
                collection_key=scope.collection_key, product=exact.product, version=exact.version
            )
        if exact and forwarded_scope(scope, resolve_backend(collection).ref).mode != "filters":
            structlog.contextvars.unbind_contextvars("audit_collection")
            return _not_searched(key, "filtered", _INDEX_CANNOT_FILTER)
        result = await search_docs(operator, query, scope=scope, collection=collection, limit=limit)
    except CollectionForbiddenError:
        return _not_searched(key, "not_entitled")
    except UnknownCollectionError:
        return _not_searched(key, "unknown_collection")
    except (CollectionNotReadyError, CollectionDisabledError):
        return _not_searched(key, "collection_not_ready")
    except CorpusUnavailable as exc:
        _log.warning("kb_wiki_search_unavailable", collection_key=key, error=str(exc))
        return _not_searched(key, "index_unavailable")
    hits = [chunk.model_copy(update={"collection": key}) for chunk in result.chunks]
    copies = await _imported_copies(operator, scope, collection, stored, hits)
    kept = [chunk for chunk in hits if chunk.chunk_id not in copies]
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


#: Why the wiki is not searched when a product or version filter cannot be applied.
_INDEX_CANNOT_FILTER: Final[str] = (
    "its index does not apply product and version as exact filters; "
    "search again without them to include it"
)


def _not_searched(key: str, reason: WikiSkipReason, why: str | None = None) -> KbWikiSearch:
    structlog.contextvars.bind_contextvars(audit_wiki_status=reason)
    return KbWikiSearch(
        collection=key,
        searched=False,
        reason=reason,
        message=_message(key, reason) if why is None else _wiki_not_searched(key, why),
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
    return _wiki_not_searched(key, why)


def _wiki_not_searched(key: str, why: str) -> str:
    return f"Your wiki (docs collection `{key}`) was not searched: {why}."


async def _imported_copies(
    operator: Operator,
    scope: DocsScope,
    collection: DocCollection,
    stored: Iterable[tuple[uuid.UUID | None, str, Mapping[str, object]]],
    hits: list[DocsChunk],
) -> set[str]:
    """Return the chunk ids of the wiki *hits* that are proven imported copies.

    See the module docstring for the rules. The stored text is read only
    for the caller's own imported entries whose page path a hit has, and a
    wiki page is read only when a hit's text is part of such an entry.
    """
    slugs_by_path = _imported_slugs(operator.tenant_id, stored)
    matched = {chunk.chunk_id: _matching_slugs(chunk, slugs_by_path) for chunk in hits}
    wanted = set().union(*matched.values())
    if not wanted:
        return set()
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(Document.source_id, Document.body).where(
                Document.tenant_id == operator.tenant_id,
                Document.source == KB_SOURCE,
                Document.source_id.in_(wanted),
            )
        )
        bodies = {slug: _plain(body) for slug, body in rows.tuples()}
    pages: dict[tuple[str, str, str], str | None] = {}
    copies: set[str] = set()
    for chunk in hits:
        text = _plain(chunk.content)
        slugs = {slug for slug in matched[chunk.chunk_id] if slug in bodies}
        if not text or chunk.read_handle is None:
            continue
        if not any(text in bodies[slug] for slug in slugs):
            continue
        page_key = _page_identity(chunk)
        if page_key not in pages:
            pages[page_key] = await _whole_page(operator, scope, collection, chunk.read_handle)
        page = pages[page_key]
        if page and any(page == bodies[slug] for slug in slugs):
            copies.add(chunk.chunk_id)
    return copies


async def _whole_page(
    operator: Operator, scope: DocsScope, collection: DocCollection, read_handle: str
) -> str | None:
    """Return the whole wiki page of a hit without its lesson header, or ``None``.

    ``None`` when the page cannot be read in full: the read is refused or
    fails, it has no text, or the backend cut it or has more of it to read.
    A read handle is bound to the filters of the search that made it, so the
    read uses the *scope* of that search, with its product and version.
    """
    try:
        read = await read_docs(
            operator, read_handle, scope=scope, collection=collection, mode="page"
        )
    except (DocsReadError, CorpusUnavailable) as exc:
        _log.info(
            "kb_wiki_page_read_failed",
            collection_key=scope.collection_key,
            reason=type(exc).__name__,
        )
        return None
    if read.text is None or read.truncated or read.next is not None:
        return None
    try:
        _header, body = parse_front_matter(read.text, "wiki page")
    except KbFileParseError:
        return None
    return _plain(body)


def _imported_slugs(
    tenant_id: uuid.UUID,
    stored: Iterable[tuple[uuid.UUID | None, str, Mapping[str, object]]],
) -> dict[str, set[str]]:
    """Map each wiki page path to the slugs of the caller's own imported copies of it."""
    slugs: dict[str, set[str]] = {}
    for owner, slug, metadata in stored:
        if owner != tenant_id:
            continue
        provenance = read_provenance(dict(metadata))
        if provenance.source_kind == "import" and provenance.relative_path:
            slugs.setdefault(provenance.relative_path.strip("/"), set()).add(slug)
    return slugs


def _matching_slugs(chunk: DocsChunk, slugs_by_path: Mapping[str, set[str]]) -> set[str]:
    """Return the slugs of the imported copies whose page path is the page of *chunk*.

    The page path is the ``document_id``, or the path after
    ``blob/<commit>/`` in the source link (``source_url`` or
    ``upstream_url``, for example ``.../-/blob/<commit>/products/x/page.md``).
    Only a path equal to an imported path matches.
    """
    if not slugs_by_path:
        return set()
    candidates = {(chunk.document_id or "").strip("/")}
    for link in (chunk.source_url, chunk.upstream_url):
        if link:
            candidates.add(_page_path_of(link))
    return set().union(*(slugs_by_path.get(path, set()) for path in candidates))


def _page_identity(chunk: DocsChunk) -> tuple[str, str, str]:
    """Return what names the wiki page of *chunk*: its document id and source links.

    Hits with the same path from another repository or another commit have
    other links, so each such page is read on its own. The part after ``#``
    (a heading or line anchor) does not name another page.
    """
    return (
        chunk.document_id or "",
        (chunk.source_url or "").partition("#")[0],
        (chunk.upstream_url or "").partition("#")[0],
    )


def _plain(text: str) -> str:
    """Return *text* in Unicode form NFC, each run of spaces and line breaks one space."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _page_path_of(link: str) -> str:
    """Return the page path after ``blob/<commit>/`` in *link*, or ``""``."""
    parts = unquote(urlsplit(link).path).strip("/").split("/")
    if "blob" not in parts:
        return ""
    page = parts[parts.index("blob") + 2 :]
    return "/".join(page)
