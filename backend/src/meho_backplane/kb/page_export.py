# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Export confirmed, generic knowledge entries as Markdown pages (#5730).

``POST /api/v1/kb/export`` calls :func:`export_pages`. The caller is a
CI job of the team wiki repo (or an operator through ``meho kb
export``). It writes the pages into the wiki on its own machine and
opens a merge request there. MEHO only reads: it writes nothing, and it
never holds Git credentials.

Which entries of the caller's tenant take part:

* Only entries with ``applies_to: product-and-version``. A fact about a
  customer, a target or a person never leaves MEHO this way.
* Only entries that an operator wrote in MEHO (provenance
  ``source_kind="direct"``). Entries that came in through a page import
  or a directory ingest are left out, so a page never goes round in a
  circle. An entry with no recorded provenance (written before MEHO
  kept it) is listed in ``skipped`` with the reason.
* ``status: confirmed`` gives a page. ``status: retired`` or
  ``status: replaced`` gives a page to remove. Any other status gives
  neither.
* A page needs the complete lesson header: ``type``, ``applies_to`` and
  ``status``, the fields the import needs. A confirmed entry without
  one of them (a direct write may leave out ``type``) is listed in
  ``skipped`` with the missing fields, so the import never refuses an
  exported page.

Each page is ``<slug>.md``: the lesson header, then the entry's text.
The header holds only the lesson header fields, in a fixed order, so a
second export of the same entries gives the same bytes. MEHO's own
settings (``shared``, provenance, owner) stay in MEHO; the import keeps
``shared`` as stored when the page comes back without it. A page whose
entry names no ``source`` gets ``source: meho://kb/<slug>``: the import
maps such a page back to the same entry, also when a reviewer moves the
file in the wiki.

Each page goes through the generic secret check of the import
(:mod:`meho_backplane.kb.secret_check`). A page that fails it is listed
in ``skipped`` with the pattern and the line, never the value.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.page_import import REQUIRED_HEADER_FIELDS, SOURCE_ENTRY_PREFIX
from meho_backplane.kb.provenance import read_provenance
from meho_backplane.kb.schemas import KB_KIND_ENTRY, KB_SOURCE
from meho_backplane.kb.secret_check import find_secret
from meho_backplane.lesson_header import LESSON_HEADER_FIELDS

__all__ = [
    "EXPORTED_APPLIES_TO",
    "KbExportPage",
    "KbExportRemoval",
    "KbExportResult",
    "KbExportSkip",
    "export_pages",
    "render_page",
]

#: The one ``applies_to`` value the export takes: generic product knowledge.
EXPORTED_APPLIES_TO: Final[str] = "product-and-version"
#: The statuses that give a page to remove.
_REMOVED_STATUSES: Final[tuple[str, ...]] = ("retired", "replaced")
_EXPORTED_STATUSES: Final[tuple[str, ...]] = ("confirmed", *_REMOVED_STATUSES)

KbRemovedStatus = Literal["retired", "replaced"]


class KbExportPage(BaseModel):
    """One page to write into the wiki."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="The file name of the page: <slug>.md.")
    slug: str
    content: str = Field(description="The whole page: the YAML lesson header, then the text.")


class KbExportRemoval(BaseModel):
    """One page to remove from the wiki: its entry is retired or replaced."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="The file name of the page: <slug>.md.")
    slug: str
    status: KbRemovedStatus


class KbExportSkip(BaseModel):
    """An entry that would be exported, but is not, and why."""

    model_config = ConfigDict(frozen=True)

    slug: str
    reason: str


class KbExportResult(BaseModel):
    """The answer of one export. Each list is sorted by slug."""

    model_config = ConfigDict(frozen=True)

    page_count: int
    remove_count: int
    skipped_count: int
    pages: list[KbExportPage]
    remove: list[KbExportRemoval]
    skipped: list[KbExportSkip]


async def export_pages(tenant_id: uuid.UUID) -> KbExportResult:
    """Return the export of *tenant_id* (see the module docstring). Writes nothing."""
    stmt = (
        select(Document.source_id, Document.body, Document.doc_metadata)
        .where(
            Document.tenant_id == tenant_id,
            Document.source == KB_SOURCE,
            Document.kind == KB_KIND_ENTRY,
            Document.doc_metadata["applies_to"].as_string() == EXPORTED_APPLIES_TO,
            Document.doc_metadata["status"].as_string().in_(_EXPORTED_STATUSES),
        )
        .order_by(Document.source_id)
    )
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).all()

    pages: list[KbExportPage] = []
    remove: list[KbExportRemoval] = []
    skipped: list[KbExportSkip] = []
    for slug, body, raw_metadata in rows:
        metadata = dict(raw_metadata or {})
        source_kind = read_provenance(metadata).source_kind
        if source_kind is None:
            skipped.append(
                KbExportSkip(
                    slug=slug,
                    reason="MEHO has no record of where this entry came from; write it again"
                    " in MEHO to export it",
                )
            )
            continue
        if source_kind != "direct":
            continue
        status = metadata["status"]
        if status != "confirmed":
            remove.append(KbExportRemoval(path=f"{slug}.md", slug=slug, status=status))
            continue
        missing = [name for name in REQUIRED_HEADER_FIELDS if metadata.get(name) in (None, "")]
        if missing:
            skipped.append(
                KbExportSkip(
                    slug=slug,
                    reason="the entry has no complete lesson header"
                    f" (missing: {', '.join(missing)}); set it in MEHO to export it",
                )
            )
            continue
        content = render_page(slug, body, metadata)
        secret = find_secret(content)
        if secret is not None:
            skipped.append(
                KbExportSkip(
                    slug=slug,
                    reason=f"the entry holds what looks like a secret ({secret}); remove it",
                )
            )
            continue
        pages.append(KbExportPage(path=f"{slug}.md", slug=slug, content=content))

    result = KbExportResult(
        page_count=len(pages),
        remove_count=len(remove),
        skipped_count=len(skipped),
        pages=pages,
        remove=remove,
        skipped=skipped,
    )
    structlog.get_logger().info(
        "kb_export_completed",
        tenant_id=str(tenant_id),
        pages=result.page_count,
        remove=result.remove_count,
        skipped=result.skipped_count,
    )
    return result


def render_page(slug: str, body: str, metadata: dict[str, Any]) -> str:
    """Return the page of one entry: the lesson header, then *body*.

    Only the lesson header fields go into the header, in the order of
    :data:`~meho_backplane.lesson_header.LESSON_HEADER_FIELDS`. MEHO's own
    keys (provenance, owner, passages) stay in MEHO. Empty values are
    left out.
    """
    values = dict(metadata)
    if values.get("source") in (None, ""):
        values["source"] = f"{SOURCE_ENTRY_PREFIX}{slug}"
    shown = [name for name in LESSON_HEADER_FIELDS if values.get(name) not in (None, "", [])]
    header = {name: values[name] for name in shown}
    text = yaml.safe_dump(header, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{text}---\n{body.rstrip()}\n"
