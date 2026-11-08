# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Import knowledge pages that the caller sends in the request (#5728).

``POST /api/v1/kb/import`` calls :func:`import_pages`. The caller is a
CI job of the team wiki repo (or a tenant admin). It has the pages on
its own machine and sends each one as ``{"path", "content"}``. MEHO
reads no folder and fetches nothing, so it never needs Git credentials.

Each page goes through these steps. A page that fails one is skipped,
and the result says why:

1. The path is a relative ``.md`` path without ``..``.
2. The generic secret check (:mod:`meho_backplane.kb.secret_check`).
3. The page starts with a YAML lesson header with ``type``,
   ``applies_to`` and ``status``. Empty header values are dropped first.
4. The header values are checked (:mod:`meho_backplane.lesson_header`).
5. A ``status: candidate`` page needs ``include_candidates``.
6. An ``applies_to: customer`` page is refused in a shared tenant.
7. The slug is the file name without ``.md``, or the slug a
   ``source: meho://kb/<slug>`` header names. So a page that left MEHO
   through the export comes back to the same entry.

Pages that claim the same slug are all skipped and listed as a conflict.
Then each page is compared with the stored entry: ``create``,
``update`` or ``unchanged``. An unchanged page is not written, so the
import is safe to run twice. A dry run stops before any write.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from meho_backplane.audit_cutover import note_preview
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.attribution import merge_attribution
from meho_backplane.kb.file_walker import (
    KbFileParseError,
    assert_header_storable,
    parse_front_matter,
)
from meho_backplane.kb.provenance import stamp_provenance
from meho_backplane.kb.revisions import write_entry
from meho_backplane.kb.schemas import (
    KB_SOURCE,
    META_LAST_UPDATED_BY_SUB,
    META_PROVENANCE,
    META_SHARED,
    InvalidKbSlugError,
    KbIngestConflict,
    KbSharedNotAllowedError,
    KbSharedSlugError,
    validate_slug,
)
from meho_backplane.kb.secret_check import find_secret
from meho_backplane.kb.shared import check_may_share, refuse_shared_slug, shared_owner_tenants
from meho_backplane.lesson_header import (
    LessonHeaderError,
    add_candidate_expiry,
    normalise_lesson_header,
)
from meho_backplane.retrieval.indexer import compute_body_hash

__all__ = [
    "MAX_PAGES",
    "MAX_PAGE_CHARS",
    "SOURCE_ENTRY_PREFIX",
    "KbImportPage",
    "KbImportPageResult",
    "KbImportRequest",
    "KbImportResult",
    "import_pages",
]

#: Most pages in one request. A bigger wiki sends several requests.
MAX_PAGES: Final[int] = 1000
#: Most characters in one page, header included.
MAX_PAGE_CHARS: Final[int] = 1_000_000
#: A ``source`` header value with this prefix names a MEHO knowledge entry.
SOURCE_ENTRY_PREFIX: Final[str] = "meho://kb/"

#: The header fields a page must have to count as having a lesson header.
_REQUIRED_HEADER: Final[tuple[str, ...]] = ("type", "applies_to", "status")
#: Stored keys that a page does not set, so the "unchanged" check skips them.
_NOT_COMPARED: Final[frozenset[str]] = frozenset({META_PROVENANCE, META_LAST_UPDATED_BY_SUB})

KbImportAction = Literal["create", "update", "unchanged", "skip"]


class KbImportPage(BaseModel):
    """One page the caller sends: its path in the wiki and its full text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=512,
        description="Path of the page in the wiki, relative, ending in .md.",
        examples=["products/<product>/<page>.md"],
    )
    content: str = Field(
        min_length=1,
        max_length=MAX_PAGE_CHARS,
        description="The whole page: the YAML lesson header, then the text.",
    )


class KbImportRequest(BaseModel):
    """POST body for ``/api/v1/kb/import``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: list[KbImportPage] = Field(min_length=1, max_length=MAX_PAGES)
    dry_run: bool = Field(
        default=False,
        description="Show what the import would do, and write nothing.",
    )
    include_candidates: bool = Field(
        default=False,
        description="Also import pages with status: candidate. Off: they are skipped.",
    )


class KbImportPageResult(BaseModel):
    """What the import did, or would do in a dry run, with one page."""

    model_config = ConfigDict(frozen=True)

    path: str
    slug: str | None = None
    action: KbImportAction
    reason: str | None = Field(
        default=None,
        description="Why the page was skipped. Set for every skipped page.",
    )


class KbImportResult(BaseModel):
    """The answer of one import: one row per page, sorted by path."""

    model_config = ConfigDict(frozen=True)

    dry_run: bool
    created_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    pages: list[KbImportPageResult]
    conflicts: list[KbIngestConflict]


class _PageSkippedError(ValueError):
    """The page is not imported; the message is the reason."""

    def __init__(self, reason: str, slug: str | None = None) -> None:
        super().__init__(reason)
        self.slug = slug


@dataclass(frozen=True)
class _Page:
    path: str
    slug: str
    body: str
    metadata: dict[str, Any]


async def import_pages(
    request: KbImportRequest,
    *,
    tenant_id: uuid.UUID,
    actor_sub: str,
) -> KbImportResult:
    """Import *request*'s pages into *tenant_id* (see the module docstring)."""
    if request.dry_run:
        # A dry run changes nothing, so the request needs no audit row.
        note_preview()
    now = datetime.now(UTC)
    results: list[KbImportPageResult] = []
    pages: list[_Page] = []
    for item in sorted(request.pages, key=lambda page: page.path):
        try:
            pages.append(_read_page(item, tenant_id, request.include_candidates))
        except _PageSkippedError as exc:
            results.append(_skipped(item.path, exc.slug, str(exc)))

    conflicts = _find_conflicts(pages)
    paths_by_slug = {conflict.slug: conflict.relative_paths for conflict in conflicts}
    for page in pages:
        if page.slug in paths_by_slug:
            reason = (
                f"conflict: the pages {', '.join(paths_by_slug[page.slug])} all use the slug"
                f" {page.slug}; none of them is written"
            )
            results.append(_skipped(page.path, page.slug, reason))
            continue
        results.append(await _import_one(page, tenant_id, actor_sub, now, request.dry_run))

    results.sort(key=lambda row: row.path)
    result = KbImportResult(
        dry_run=request.dry_run,
        created_count=_count(results, "create"),
        updated_count=_count(results, "update"),
        unchanged_count=_count(results, "unchanged"),
        skipped_count=_count(results, "skip"),
        pages=results,
        conflicts=conflicts,
    )
    structlog.get_logger().info(
        "kb_import_completed",
        tenant_id=str(tenant_id),
        created=result.created_count,
        updated=result.updated_count,
        unchanged=result.unchanged_count,
        skipped=result.skipped_count,
        dry_run=request.dry_run,
    )
    return result


def _read_page(item: KbImportPage, tenant_id: uuid.UUID, include_candidates: bool) -> _Page:
    """Check one page and return it ready to write, or raise :class:`_PageSkippedError`."""
    _check_path(item.path)
    secret = find_secret(item.content)
    if secret is not None:
        raise _PageSkippedError(f"the page holds what looks like a secret ({secret}); remove it")
    metadata, body = _read_header(item)
    if metadata["status"] == "candidate" and not include_candidates:
        raise _PageSkippedError("status is candidate; send include_candidates to import candidates")
    if metadata["applies_to"] == "customer" and (
        tenant_id in shared_owner_tenants() or metadata.get(META_SHARED) is True
    ):
        raise _PageSkippedError(
            "applies_to is customer, and a customer fact never goes into a shared tenant;"
            " keep it in the engagement's project folder"
        )
    try:
        check_may_share(tenant_id, metadata)
        slug = validate_slug(_slug_for(item.path, metadata))
    except (LessonHeaderError, KbSharedNotAllowedError, InvalidKbSlugError) as exc:
        raise _PageSkippedError(str(exc)) from exc
    if not body.strip():
        raise _PageSkippedError("the page has no text below the header", slug)
    return _Page(path=item.path, slug=slug, body=body, metadata=metadata)


def _check_path(path: str) -> None:
    parts = PurePosixPath(path).parts
    if (
        "\\" in path
        or path.startswith("/")
        or not path.endswith(".md")
        or ".." in parts
        or PurePosixPath(path).stem == ""
    ):
        raise _PageSkippedError("the path must be a relative path to a .md file, without ..")


def _read_header(item: KbImportPage) -> tuple[dict[str, Any], str]:
    """Return the checked lesson header and the body of *item*."""
    try:
        raw, body = parse_front_matter(item.content, item.path)
    except KbFileParseError as exc:
        raise _PageSkippedError(str(exc)) from exc
    header = {key: value for key, value in raw.items() if not _is_empty(value)}
    missing = [name for name in _REQUIRED_HEADER if name not in header]
    if missing:
        raise _PageSkippedError(
            "the page has no lesson header: it needs type, applies_to and status"
            f" (missing: {', '.join(missing)})"
        )
    try:
        metadata = normalise_lesson_header(header, allowed_reserved=frozenset({META_PROVENANCE}))
        assert_header_storable(metadata, item.path)
    except (LessonHeaderError, KbFileParseError) as exc:
        raise _PageSkippedError(str(exc)) from exc
    return metadata, body


def _is_empty(value: object) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value is None or value == [] or value == {}


def _slug_for(path: str, metadata: dict[str, Any]) -> str:
    """The entry the ``source`` header names, else the file name without ``.md``."""
    source = metadata.get("source")
    if isinstance(source, str) and source.startswith(SOURCE_ENTRY_PREFIX):
        return source.removeprefix(SOURCE_ENTRY_PREFIX)
    return PurePosixPath(path).stem


def _find_conflicts(pages: Sequence[_Page]) -> list[KbIngestConflict]:
    paths_by_slug: dict[str, list[str]] = defaultdict(list)
    for page in pages:
        paths_by_slug[page.slug].append(page.path)
    return [
        KbIngestConflict(slug=slug, relative_paths=sorted(paths))
        for slug, paths in sorted(paths_by_slug.items())
        if len(paths) > 1
    ]


async def _import_one(
    page: _Page,
    tenant_id: uuid.UUID,
    actor_sub: str,
    now: datetime,
    dry_run: bool,
) -> KbImportPageResult:
    """Compare *page* with the stored entry, then write it unless *dry_run*."""
    try:
        await refuse_shared_slug(tenant_id, page.slug)
        action = await _classify(page, tenant_id, actor_sub, now)
        if action != "unchanged" and not dry_run:
            await write_entry(
                tenant_id=tenant_id,
                slug=page.slug,
                body=page.body,
                metadata_for=lambda existing: _stored_metadata(page, existing, actor_sub, now),
            )
    except KbSharedSlugError as exc:
        return _skipped(page.path, page.slug, str(exc))
    except Exception as exc:
        structlog.get_logger().exception(
            "kb_import_page_failed", path=page.path, tenant_id=str(tenant_id)
        )
        return _skipped(
            page.path, page.slug, f"the write failed ({type(exc).__name__}); nothing was stored"
        )
    return KbImportPageResult(path=page.path, slug=page.slug, action=action)


async def _classify(
    page: _Page, tenant_id: uuid.UUID, actor_sub: str, now: datetime
) -> KbImportAction:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(Document.body_hash, Document.doc_metadata).where(
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                    Document.source_id == page.slug,
                )
            )
        ).one_or_none()
    if row is None:
        return "create"
    stored = dict(row.doc_metadata or {})
    wanted = _stored_metadata(page, stored, actor_sub, now)
    if row.body_hash == compute_body_hash(page.body) and _comparable(wanted) == _comparable(stored):
        return "unchanged"
    return "update"


def _stored_metadata(
    page: _Page, existing: dict[str, Any] | None, actor_sub: str, now: datetime
) -> dict[str, Any]:
    """The metadata a write of *page* stores: header, owner, provenance."""
    metadata = add_candidate_expiry(page.metadata, existing=existing, now=now)
    attributed = merge_attribution(
        caller_metadata=metadata,
        existing_metadata=existing,
        actor_sub=actor_sub,
        created=existing is None,
    )
    return stamp_provenance(
        attributed if attributed is not None else metadata,
        source_kind="import",
        relative_path=page.path,
        ingested_at=now,
    )


def _comparable(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key not in _NOT_COMPARED}


def _skipped(path: str, slug: str | None, reason: str) -> KbImportPageResult:
    return KbImportPageResult(path=path, slug=slug, action="skip", reason=reason)


def _count(results: Sequence[KbImportPageResult], action: KbImportAction) -> int:
    return sum(1 for row in results if row.action == action)
