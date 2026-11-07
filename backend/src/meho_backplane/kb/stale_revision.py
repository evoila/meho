# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The refusal of a kb write that did not read the current revision.

Every update to a kb entry names the revision it read
(``expected_revision``). When the entry has changed since, or the slug
exists and the write names no revision, the write is refused with
:class:`KbStaleRevisionError`. The error carries a
:class:`KbStaleRevisionDetail`: the current revision, who wrote it and
when, and what changed since the revision the writer read. REST, MCP,
the CLI and the console all show this same detail.

What changed is the metadata keys that differ and a short line-by-line
difference of the body. Both are cut to a fixed size
(:data:`MAX_DIFF_LINES`, :data:`MAX_DIFF_CHARS`, :data:`MAX_CHANGED_KEYS`).
The body difference and the key names are stored text, so
:meth:`KbStaleRevisionDetail.to_payload` marks them as untrusted text
(data, not instructions), like a knowledge read.
"""

from __future__ import annotations

import difflib
import json
import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, KbRevision
from meho_backplane.kb.schemas import (
    KB_SOURCE,
    META_LAST_UPDATED_BY_SUB,
    META_PROVENANCE,
)
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = [
    "MAX_CHANGED_KEYS",
    "MAX_DIFF_CHARS",
    "MAX_DIFF_LINES",
    "KbStaleRevisionDetail",
    "KbStaleRevisionError",
    "build_stale_detail",
    "metadata_changed_keys",
]

#: The body difference shows at most this many lines and characters.
MAX_DIFF_LINES: Final[int] = 40
MAX_DIFF_CHARS: Final[int] = 2000
#: At most this many metadata keys are named.
MAX_CHANGED_KEYS: Final[int] = 20

#: Metadata keys that change on every write without changing the entry:
#: the write time inside the provenance block, and the writer's name.
_VOLATILE_KEYS: Final[frozenset[str]] = frozenset({META_PROVENANCE, META_LAST_UPDATED_BY_SUB})

StaleReason = Literal["stale", "exists", "gone"]


class KbStaleRevisionDetail(BaseModel):
    """What a refused write is told about the entry as it is now.

    ``cause`` is ``stale`` (the entry changed after the revision the
    writer read), ``exists`` (the write named no revision but the slug
    exists) or ``gone`` (the writer named a revision but the entry is
    deleted). ``changed_metadata_keys`` and ``body_diff`` are ``None``
    when the revision the writer read is not known, for example on
    ``exists``.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    cause: StaleReason
    expected_revision_id: uuid.UUID | None
    current_revision_id: uuid.UUID | None
    current_revision_number: int | None
    changed_by: str | None
    changed_at: datetime | None
    changed_metadata_keys: list[str] | None
    body_diff: str | None
    body_diff_truncated: bool

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON answer, with stored text marked as untrusted."""
        data = self.model_dump(mode="json")
        data["error"] = "stale_revision"
        if data["body_diff"] is not None:
            data["body_diff"] = wrap_untrusted_text(data["body_diff"])
        if data["changed_metadata_keys"] is not None:
            data["changed_metadata_keys"] = [
                wrap_untrusted_text(key) for key in data["changed_metadata_keys"]
            ]
        if data["changed_by"] is not None:
            data["changed_by"] = wrap_untrusted_text(data["changed_by"])
        return data


class KbStaleRevisionError(Exception):
    """A kb write did not name the current revision of the entry."""

    def __init__(self, detail: KbStaleRevisionDetail) -> None:
        self.detail = detail
        current = detail.current_revision_id
        if detail.cause == "exists":
            text = (
                f"the knowledge entry {detail.slug!r} exists already; "
                f"read it and send expected_revision {current} to change it"
            )
        elif detail.cause == "gone":
            text = f"the knowledge entry {detail.slug!r} was deleted after you read it"
        else:
            text = (
                f"the knowledge entry {detail.slug!r} changed after the revision you read; "
                f"the current revision is {current}"
            )
        super().__init__(text)


def metadata_changed_keys(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> list[str] | None:
    """Return the sorted metadata keys whose value differs; ``None`` when *before* is unknown.

    A key counts as changed when it is added or removed, also with the
    value ``null``, or when its JSON value differs. The JSON types are
    kept apart: ``false`` is not ``0``, and ``"1"`` is not ``1``. The
    provenance block and ``last_updated_by_sub`` are left out: they
    change on every write.
    """
    if before is None or after is None:
        return None
    keys = (set(before) | set(after)) - _VOLATILE_KEYS
    return sorted(key for key in keys if _json_of(before, key) != _json_of(after, key))


def _json_of(metadata: dict[str, Any], key: str) -> str | None:
    """The canonical JSON text of one metadata value; ``None`` when the key is absent."""
    if key not in metadata:
        return None
    return json.dumps(metadata[key], sort_keys=True, default=str)


def _body_diff(before: str, after: str) -> tuple[str, bool]:
    """Return a short unified line difference and whether it was cut."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), "read", "current", n=0, lineterm=""
        )
    )
    truncated = len(lines) > MAX_DIFF_LINES
    text = "\n".join(lines[:MAX_DIFF_LINES])
    if len(text) > MAX_DIFF_CHARS:
        text, truncated = text[:MAX_DIFF_CHARS], True
    return text, truncated


async def build_stale_detail(
    tenant_id: uuid.UUID,
    slug: str,
    expected_revision_id: uuid.UUID | None,
) -> KbStaleRevisionDetail:
    """Read the entry as it is now and describe how it differs from what the writer read."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        head = (
            await session.execute(
                select(Document.id, Document.body, Document.doc_metadata).where(
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                    Document.source_id == slug,
                )
            )
        ).one_or_none()
        if head is None:
            return _detail(slug, "gone", expected_revision_id)
        current = (
            await session.execute(
                select(KbRevision)
                .where(KbRevision.entry_id == head.id)
                .order_by(KbRevision.revision_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        read = None
        if expected_revision_id is not None:
            read = (
                await session.execute(
                    select(KbRevision).where(
                        KbRevision.id == expected_revision_id,
                        KbRevision.entry_id == head.id,
                    )
                )
            ).scalar_one_or_none()

    metadata = dict(head.doc_metadata)
    stored = current.entry_metadata if current is not None else None
    writer = (stored if stored is not None else metadata).get(META_LAST_UPDATED_BY_SUB)
    keys: list[str] | None = None
    diff: str | None = None
    truncated = False
    if read is not None:
        keys = metadata_changed_keys(read.entry_metadata, metadata)
        if keys is not None and len(keys) > MAX_CHANGED_KEYS:
            keys, truncated = keys[:MAX_CHANGED_KEYS], True
        diff, cut = _body_diff(read.body, head.body)
        truncated = truncated or cut
    return KbStaleRevisionDetail(
        slug=slug,
        cause="stale" if expected_revision_id is not None else "exists",
        expected_revision_id=expected_revision_id,
        current_revision_id=current.id if current is not None else None,
        current_revision_number=current.revision_number if current is not None else None,
        changed_by=writer if isinstance(writer, str) else None,
        changed_at=current.created_at if current is not None else None,
        changed_metadata_keys=keys,
        body_diff=diff,
        body_diff_truncated=truncated,
    )


def _detail(
    slug: str, cause: StaleReason, expected_revision_id: uuid.UUID | None
) -> KbStaleRevisionDetail:
    """Build a detail for an entry that no longer exists."""
    return KbStaleRevisionDetail(
        slug=slug,
        cause=cause,
        expected_revision_id=expected_revision_id,
        current_revision_id=None,
        current_revision_number=None,
        changed_by=None,
        changed_at=None,
        changed_metadata_keys=None,
        body_diff=None,
        body_diff_truncated=False,
    )
