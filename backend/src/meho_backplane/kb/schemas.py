# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 frozen models + the slug validation contract for the kb layer.

Every model here is ``ConfigDict(frozen=True)`` so a handler that
accidentally mutates a returned entry surfaces as a Pydantic error
instead of a silently-modified response. The frozen flag covers
attribute reassignment only -- the ``metadata`` dict and the
``errors`` list inside :class:`KbIngestionResult` remain Python-
mutable, but the build path for those collections is local to
:mod:`meho_backplane.kb.service` and there are no read-then-write
mutation paths in callers.

The :data:`KB_SOURCE` / :data:`KB_KIND_ENTRY` constants are the
load-bearing string contract against the ``documents`` table. The
G0.4 substrate uses these strings as filter values for retrieval
scoping, so changing them in this module is a documents-table data
migration -- treat as if they were column names.

The slug regex (:data:`SLUG_PATTERN`) is the operator-facing
identifier contract:

* Starts with a lowercase ASCII letter (so slugs sort cleanly and
  stay URL-friendly without percent-encoding).
* Ends with a lowercase ASCII letter or digit (so slugs cannot end
  with a separator character that would look ambiguous in a URL or
  CLI verb).
* Middle is lowercase ASCII letters, digits, hyphens, or **dots**.

Dots in the middle are load-bearing for the consumer's real kb where
filenames carry version numbers (``vcenter-9.0-snapshot-revert.md``
→ slug ``vcenter-9.0-snapshot-revert``). The task body's own example
exhibits this shape; the regex in the task body excluded dots, which
this module relaxes after confirming the consumer corpus relies on
the dotted form. Single-character slugs (``a``, ``9``) are
explicitly accepted because the consumer's corpus does not preclude
them and the start-and-end-anchor pattern would otherwise reject
them as a side effect of requiring both anchors.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict

__all__ = [
    "KB_KIND_ENTRY",
    "KB_SOURCE",
    "META_CREATED_BY_SUB",
    "META_LAST_UPDATED_BY_SUB",
    "SLUG_PATTERN",
    "InvalidKbSlugError",
    "KbEntry",
    "KbEntrySearchHit",
    "KbIngestionResult",
    "validate_slug",
]


#: The ``documents.source`` value every kb row carries. Load-bearing
#: filter for hybrid retrieval scoping; changing the string is a
#: data-migration event.
KB_SOURCE: Final[str] = "kb"

#: Metadata keys that carry per-entry write attribution. Stored inside
#: ``documents.doc_metadata`` (not as new columns) so attribution rides
#: the existing JSONB shape and surfaces on every read surface that
#: already returns ``metadata`` -- ``GET /api/v1/kb/{slug}``, the list
#: preview, ``POST /api/v1/kb``, and ``POST /api/v1/retrieve`` hits --
#: with no schema migration. The service writes these keys; callers
#: that pass them in a create-body ``metadata`` are stripped (the OIDC
#: ``sub`` is the trust boundary, not caller-supplied JSON) so an
#: operator cannot forge authorship.
#:
#: ``created_by_sub`` is set once on first index and preserved verbatim
#: across every subsequent overwrite (even a cross-principal one);
#: ``last_updated_by_sub`` is rewritten to the acting principal on
#: every write. Together they make a kb row self-describing about who
#: wrote it and who last mutated it without an audit-log correlation.
META_CREATED_BY_SUB: Final[str] = "created_by_sub"
META_LAST_UPDATED_BY_SUB: Final[str] = "last_updated_by_sub"

#: The ``documents.kind`` value every kb entry carries. Distinct from
#: future kb-adjacent rows (kb-index, kb-collection) that would each
#: pick their own ``kind`` value within the ``source='kb'`` namespace.
KB_KIND_ENTRY: Final[str] = "kb-entry"

#: Slug regex. Anchored start + end. Lowercase letter as the leading
#: character; lowercase letter or digit as the trailing character;
#: middle is lowercase letters, digits, hyphens, or dots. Single-
#: character slugs satisfy the regex through the ``[a-z]`` alternative
#: that bypasses the middle group. Matches the consumer kb's dotted
#: version-number convention (``vcenter-9.0-snapshot-revert``).
SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z](?:[a-z0-9.\-]*[a-z0-9])?$")


class InvalidKbSlugError(ValueError):
    """The supplied slug does not match :data:`SLUG_PATTERN`.

    Subclass of :class:`ValueError` so a caller treating the failure
    as a request-input problem can ``except ValueError:`` without
    importing the kb module; the API surface (T2) and the CLI (T4)
    catch this explicitly and translate to a 422 / nonzero exit.
    """


def validate_slug(slug: str) -> str:
    """Return *slug* unchanged when valid; raise :class:`InvalidKbSlugError` otherwise.

    Pure function -- the public sink for every code path that turns
    operator input (filename, front-matter override, API request
    body) into a ``documents.source_id`` value. Centralising the
    check in one function lets future contract changes (a stricter
    length cap, a tighter character set) ship in one place.
    """
    if not SLUG_PATTERN.match(slug):
        raise InvalidKbSlugError(
            f"slug {slug!r} does not match {SLUG_PATTERN.pattern!r} -- "
            "must start with [a-z], end with [a-z0-9], and contain only "
            "lowercase letters, digits, hyphens, or dots"
        )
    return slug


class KbEntry(BaseModel):
    """One kb entry -- mirrors a row in the ``documents`` table.

    Returned by :meth:`KbService.get_entry`, :meth:`KbService.create_entry`,
    and (in list form) :meth:`KbService.list_entries`. ``slug`` is the
    operator-facing identifier (``documents.source_id`` on the SQL
    side); the underlying ``Document.id`` UUID is also surfaced for
    cross-correlation with audit-log rows and future MCP resource
    URIs.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    slug: str
    body: str
    metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime


class KbEntrySearchHit(BaseModel):
    """One ranked search hit returned by :meth:`KbService.search_entries`.

    Adapts :class:`~meho_backplane.retrieval.retriever.RetrievalHit`
    to a kb-shaped vocabulary (``slug`` rather than ``source_id``,
    ``snippet`` rather than the full body). Per-signal scores and
    ranks are preserved so callers tuning retrieval quality can read
    them; the absence of either signal (``bm25_score=None`` /
    ``cosine_score=None``) means the hit only appeared in the other
    signal's top-:data:`~meho_backplane.retrieval.retriever.CANDIDATE_LIMIT`.

    ``snippet`` is the first ~200 characters of the body -- enough to
    let an operator (or the agent's downstream reasoning) decide
    whether a full ``get_entry`` is warranted. The full body is
    available through :meth:`KbService.get_entry` keyed on the
    returned ``slug``.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    snippet: str
    metadata: dict[str, object]
    fused_score: float
    bm25_score: float | None
    cosine_score: float | None
    bm25_rank: int | None
    cosine_rank: int | None


class KbIngestionResult(BaseModel):
    """Summary of one :meth:`KbService.ingest_directory` run.

    The four counters partition every discovered ``.md`` file into
    exactly one of four buckets:

    * ``inserted_count`` -- file's slug had no prior row in the tenant;
      a new document row was created and embedded.
    * ``updated_count`` -- file's slug had a prior row whose
      ``body_hash`` differed from the current body; the row was
      re-embedded and updated.
    * ``skipped_count`` -- file's slug had a prior row whose
      ``body_hash`` matched (dominant case on the second run against
      an unchanged corpus -- the body-hash short-circuit from
      G0.4-T3 means no embedding compute happened).
    * ``error_count`` -- the file could not be read or parsed
      (binary masquerading as ``.md``, invalid slug, malformed
      front-matter). The matching error message is appended to
      :attr:`errors`; the ingestion run continues with the remaining
      files.

    Sum invariant: ``inserted_count + updated_count + skipped_count
    + error_count == <total discovered .md files>``.

    The ``errors`` list is bounded in practice by the corpus size --
    no per-file error string exceeds ~200 chars (the file path plus a
    short reason), so even a worst-case all-error run on a 1000-file
    corpus stays under ~200 KB of allocated memory. Truncation /
    sampling can land in v0.2.next if real corpora drive that.
    """

    model_config = ConfigDict(frozen=True)

    inserted_count: int
    updated_count: int
    skipped_count: int
    error_count: int
    errors: list[str]
