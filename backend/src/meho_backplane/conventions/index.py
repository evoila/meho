# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded own-tenant convention index: page shape and opaque cursor (#3755).

The index lists a tenant's conventions without their bodies. Each entry
says whether MEHO puts the convention in the MCP ``initialize``
instructions (``included``), leaves it out for lack of room
(``dropped``), leaves it out because it is too large even alone
(``dropped_oversize``), or never sends it because its kind is not
``operational`` (``not_in_preamble``).

Pages hold at most :data:`INDEX_PAGE_SIZE` entries. The cursor that
points at the next page carries three things:

* the tenant id, so a cursor only works for the tenant that got it;
* the index ``revision`` (a short digest of every row's id and
  ``updated_at``), so a walk across pages reads one snapshot -- if any
  convention changes between pages, the next page is refused and the
  client starts again from the first page instead of silently skipping
  or repeating rows;
* the offset of the next page inside that snapshot.

The cursor is base64url JSON, the same opaque shape the audit query
cursor uses. It is not signed: it carries no secret, and every read
still filters on the tenant from the caller's token, so a hand-made
cursor can only ever reach the caller's own conventions. A cursor for
another tenant gets the same error as a broken one, so the error does
not tell the caller anything about other tenants.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.conventions.schemas import ConventionKind

__all__ = [
    "INDEX_PAGE_SIZE",
    "ConventionIndexEntry",
    "ConventionIndexPage",
    "IndexAvailability",
    "IndexCursor",
    "InvalidIndexCursorError",
    "StaleIndexCursorError",
    "decode_index_cursor",
    "encode_index_cursor",
    "index_availability",
    "index_revision",
]

#: Most entries one index page returns. Below the ~50-row point where
#: MEHO switches set-shaped results to result handles, and small enough
#: that a page of 200-character titles stays a few KB.
INDEX_PAGE_SIZE: Final[int] = 25


class IndexAvailability(StrEnum):
    """Whether MEHO sends a convention in the MCP ``initialize`` instructions.

    This describes what MEHO sends. A client may ignore instructions, so
    ``included`` is not proof that an agent read or applied the rule.
    """

    INCLUDED = "included"
    DROPPED = "dropped"
    DROPPED_OVERSIZE = "dropped_oversize"
    NOT_IN_PREAMBLE = "not_in_preamble"


class ConventionIndexEntry(BaseModel):
    """One index row. Never carries the convention body."""

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str
    kind: str
    priority: int
    updated_at: datetime
    availability: IndexAvailability


class ConventionIndexPage(BaseModel):
    """One bounded page of the tenant's convention index."""

    model_config = ConfigDict(frozen=True)

    revision: str
    total: int = Field(ge=0)
    entries: list[ConventionIndexEntry] = Field(max_length=INDEX_PAGE_SIZE)
    next_cursor: str | None
    max_tokens: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    over_budget: bool


class InvalidIndexCursorError(ValueError):
    """The cursor is broken, made for another tenant, or out of range.

    All of these share one error so the caller learns nothing about
    other tenants from the rejection.
    """


class StaleIndexCursorError(ValueError):
    """The caller's own conventions changed since the cursor was made."""


class IndexCursor(BaseModel):
    """Decoded cursor: tenant, snapshot revision, and next-page offset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    t: uuid.UUID
    r: str = Field(min_length=1, max_length=64)
    o: int = Field(ge=1)


def index_availability(
    slug: str,
    kind: str,
    included: frozenset[str],
    oversize: frozenset[str],
) -> IndexAvailability:
    """Map one row onto its availability, given the pack's slug sets."""
    if kind != ConventionKind.OPERATIONAL.value:
        return IndexAvailability.NOT_IN_PREAMBLE
    if slug in included:
        return IndexAvailability.INCLUDED
    if slug in oversize:
        return IndexAvailability.DROPPED_OVERSIZE
    return IndexAvailability.DROPPED


def index_revision(rows: Iterable[tuple[uuid.UUID, datetime]]) -> str:
    """Return a short digest over every row's ``(id, updated_at)`` in index order.

    Any create, update, or delete changes the digest, and so does a
    change in order (a priority edit also bumps ``updated_at``).
    """
    digest = hashlib.sha256()
    for row_id, updated_at in rows:
        digest.update(f"{row_id}|{updated_at.isoformat()}\n".encode())
    return digest.hexdigest()[:16]


def encode_index_cursor(cursor: IndexCursor) -> str:
    """Encode *cursor* as an opaque URL-safe base64 token (no ``/``)."""
    raw = cursor.model_dump_json().encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_index_cursor(
    token: str,
    *,
    tenant_id: uuid.UUID,
    revision: str,
    total: int,
) -> IndexCursor:
    """Decode *token* and check it against the caller's current snapshot.

    Raises :class:`InvalidIndexCursorError` when the token is broken,
    names another tenant, or points past the end of the snapshot it
    claims. Raises :class:`StaleIndexCursorError` when the token is for
    this tenant but its snapshot revision no longer matches. The tenant
    check runs before the revision check, so a foreign cursor never
    reaches the "changed" answer.
    """
    try:
        raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True)
        cursor = IndexCursor.model_validate(json.loads(raw.decode("utf-8")))
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise InvalidIndexCursorError("invalid index cursor") from exc
    if cursor.t != tenant_id:
        raise InvalidIndexCursorError("invalid index cursor")
    if cursor.r != revision:
        raise StaleIndexCursorError("the convention index changed since this cursor was made")
    if cursor.o >= total:
        raise InvalidIndexCursorError("invalid index cursor")
    return cursor
