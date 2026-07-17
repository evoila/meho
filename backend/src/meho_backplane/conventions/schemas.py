# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas + token-budget heuristic for tenant conventions.

Initiative #229 (G7.1), Task #314 (T2). The package's two responsibilities:

* **Request / response models** -- :class:`Convention` (read),
  :class:`ConventionSummary` (list-row), :class:`ConventionCreate`
  (POST body), :class:`ConventionUpdate` (PATCH body), and
  :class:`ConventionHistoryEntry` (history list-row). All pydantic v2
  ``frozen=True`` + ``extra="forbid"`` so a typo (``"bodytext"``)
  trips 422 at the framework layer rather than silently being
  dropped. ``ConfigDict(from_attributes=True)`` lets the FastAPI
  handlers return the SQLAlchemy ORM object directly.

* **Token-budget heuristic** -- :func:`estimate_tokens` plus
  :data:`DEFAULT_MAX_PREAMBLE_TOKENS`. The write-time 422 validation
  on POST/PATCH and the read-time priority-ranked packer T4 (#316)
  ships **both** read through this helper so the two sites cannot
  drift. A divergence would mean a convention that POSTed
  successfully gets silently dropped at every future preamble
  assembly -- the exact failure mode the issue body calls out
  ("``kubectl apply --dry-run=server`` discipline -- fail at write,
  not silently at every future preamble assembly").

Why a chars-per-token heuristic instead of a real tokenizer
====================================================================

The session-preamble assembler (T4) runs on every MCP ``initialize``
for every authenticated agent; loading a tokenizer per call costs
~50ms of import time the first call (the cl100k_base tables) plus
~5ms per text. Both costs are negligible against the preamble
budget contract (the preamble caps at ~600 tokens) and the absolute
worst-case bound the heuristic blunders into is bounded: 3.3
chars/token is the conservative direction (it estimates *more*
tokens than a real tokenizer would for ASCII English, so a write
that *passes* the heuristic check is guaranteed to fit any real
tokenizer's count, and a write that fails was unlikely to fit
anyway). The heuristic also has no version-skew risk -- a tokenizer
upgrade between MEHO releases would otherwise silently change which
convention bodies are accepted.

Issue body cites the heuristic explicitly: "estimate its token cost
(the same heuristic T4's assembler uses)" and "~3.3 chars/token per
the doc". locked-decisions.md §G7 doc (referenced in the
[tenant_conventions](../../../../docs/codebase/tenant_conventions.md)
codebase artifact) names the constant.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_MAX_PREAMBLE_TOKENS",
    "TOKEN_CHAR_RATIO",
    "BudgetStatus",
    "Convention",
    "ConventionCreate",
    "ConventionHistoryEntry",
    "ConventionKind",
    "ConventionListResponse",
    "ConventionSummary",
    "ConventionUpdate",
    "PreambleInclusion",
    "estimate_tokens",
]


#: Default preamble token budget. T4's ``assemble_preamble`` packs
#: ``kind='operational'`` conventions highest-priority-first up to
#: this cap, dropping lowest-priority entries whole on overflow. T2
#: uses the same cap as the single-entry rejection threshold: a
#: convention whose own estimated token cost exceeds the budget
#: cannot fit at any priority, so failing at POST/PATCH time
#: surfaces the bug loudly rather than silently every preamble
#: assembly thereafter. The constant is set at 600 per the v0.1-spec
#: §"Memory / context layer" (L457-487) baseline -- well below
#: typical context windows but generous for ~10-20 operational rules.
DEFAULT_MAX_PREAMBLE_TOKENS: Final[int] = 600


#: Chars-per-token ratio used by :func:`estimate_tokens`. 3.3 is the
#: conservative direction for ASCII English: real BPE tokenizers
#: (cl100k_base, o200k_base) typically average ~4 chars/token for
#: this content shape. Using a lower ratio means the estimator
#: over-counts tokens, so a body that *passes* the 422 check is
#: comfortably within the real-tokenizer budget. A divergence in the
#: other direction (estimator under-counting) would silently let
#: oversize bodies through and the preamble packer would drop them
#: at runtime -- the precise failure mode the write-time validation
#: exists to prevent.
TOKEN_CHAR_RATIO: Final[float] = 3.3


def estimate_tokens(text: str) -> int:
    """Estimate the token cost of *text* via the chars-per-token heuristic.

    Returns ``ceil(len(text) / TOKEN_CHAR_RATIO)`` -- ceiling rather
    than floor so a body that's exactly on the boundary rounds up
    (the conservative direction the budget contract wants). Empty
    string returns 0; the helper never returns a negative integer.

    Used by both the T2 (#314) POST/PATCH 422 validation (a single
    ``operational`` body whose estimate exceeds the preamble budget
    is rejected at write time) and the T4 (#316) preamble assembler
    (priority-ranked packing up to the budget). Sharing the same
    helper across the two sites is the single source of truth for
    the budget contract -- a future tokenizer swap-in lands here and
    both consumers pick it up.
    """
    if not text:
        return 0
    # ``math.ceil`` over plain ``//`` so the chars-per-token floor
    # never under-counts a body sitting exactly on a token boundary
    # (the budget contract is "fits within N tokens"; rounding down
    # would let a 600.4-token body claim 600 and silently overflow).
    return math.ceil(len(text) / TOKEN_CHAR_RATIO)


class ConventionKind(StrEnum):
    """Closed vocabulary for ``TenantConvention.kind``.

    The DB column itself is free-form ``TEXT`` per the issue body's
    Out of scope ("DB-level enum on ``kind``" deferred); this enum
    is the API-layer single line of defence. A request with
    ``kind="garbage"`` trips 422 at pydantic parse time before the
    handler runs -- the same surface shape pydantic raises for
    every other validation failure, so callers can branch on 4xx
    uniformly. Per the issue body, only ``operational`` conventions
    are packed into the session preamble; ``workflow`` /
    ``reference`` are reference material the operator surfaces on
    demand and are exempt from the over-budget 422 rejection.
    """

    OPERATIONAL = "operational"
    WORKFLOW = "workflow"
    REFERENCE = "reference"


# ---------------------------------------------------------------------------
# Request bodies (POST / PATCH)
# ---------------------------------------------------------------------------


class ConventionCreate(BaseModel):
    """POST body for ``/api/v1/conventions``.

    ``slug`` is the operator-visible identifier the URL / CLI / audit
    log all reference; it is the natural key within a tenant (the
    composite-unique index in T1's migration enforces uniqueness).
    Bounded at 128 characters and constrained to a URL-safe shape
    (lowercase ASCII, digits, hyphen) so audit log paths stay
    grep-friendly and no operator can sneak a slash through. The
    title and body are bounded only by a generous upper limit; the
    real budget gate is the over-budget 422 in the route handler,
    which runs :func:`estimate_tokens` against ``body`` before
    insert and rejects any single ``operational`` entry exceeding
    :data:`DEFAULT_MAX_PREAMBLE_TOKENS`.

    ``priority`` is optional with a default of 0; the SmallInteger
    column on :class:`TenantConvention` bounds the range to
    -32768..32767. Per the issue body, ``priority`` is the ranking
    key T4's preamble packer uses to drop low-priority entries
    whole on budget overflow -- higher value wins. The default of
    0 matches the column's ``server_default`` so omitting the field
    round-trips identically through create + show.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=64_000)
    kind: ConventionKind
    # Match the SmallInteger column range exactly -- a value outside
    # -32768..32767 would trip an IntegrityError on PG (and silently
    # truncate on SQLite) for no gain; refusing it at the API layer
    # with 422 is the better diagnostic.
    priority: int = Field(default=0, ge=-32768, le=32767)


class ConventionUpdate(BaseModel):
    """PATCH body for ``/api/v1/conventions/{slug}``.

    All fields optional -- PATCH semantics means "update only what's
    provided". Pydantic v2's :attr:`BaseModel.model_fields_set`
    distinguishes "field absent from JSON" from "field present with
    null", and the route handler uses that view to apply only the
    explicitly-set keys to the ORM row. The kind discriminator is
    excluded from the PATCH surface: changing a convention's kind
    in-place would silently change its preamble-inclusion behaviour
    (an ``operational`` rule becoming ``reference`` would disappear
    from every future preamble without an audit signal); operators
    delete + recreate to switch kind, which produces a clean two-
    row history trail.

    ``priority`` is included because reranking is the most common
    edit shape (a rule that became more / less urgent without its
    text changing). ``slug`` is not in the PATCH surface either --
    renaming a convention is a delete + recreate as well, since
    the audit log and history rows reference the old slug by
    natural key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=64_000)
    priority: int | None = Field(default=None, ge=-32768, le=32767)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ConventionSummary(BaseModel):
    """List-row representation returned by ``GET /api/v1/conventions``.

    Lighter than :class:`Convention` -- omits the full ``body`` so
    a list of 20 conventions doesn't materialise 20 KB of rule
    text on every list call. The CLI's ``meho conventions list``
    uses this shape to render a one-line-per-convention table;
    ``meho conventions show <slug>`` reaches for the full
    :class:`Convention` shape via the per-slug GET route.
    ``from_attributes=True`` lets the handler return the SQLAlchemy
    ORM object directly.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    slug: str
    title: str
    kind: str
    priority: int
    created_by_sub: str | None
    created_at: datetime
    updated_at: datetime


class PreambleInclusion(BaseModel):
    """Whether a just-written convention reaches the session preamble.

    G0.14-T8 (#1149, signal 18) -- the post-write feedback shape
    ``POST /api/v1/conventions`` and ``PATCH /api/v1/conventions/{slug}``
    attach to the response when the convention is ``kind='operational'``.
    Without it, the operator gets a ``201`` (or ``200`` on PATCH), assumes
    the rule is in effect, and the agent silently never sees it because
    the preamble packer dropped it on budget overflow.

    Fields:

    * ``included`` -- ``True`` when the slug landed in the assembled
      preamble (after the priority-ranked pack against
      :data:`DEFAULT_MAX_PREAMBLE_TOKENS`). ``False`` when the packer
      dropped it whole on overflow.
    * ``position`` -- 1-based index of the convention in the assembled
      preamble's packed order (the packer iterates ``priority DESC,
      created_at ASC``; ``position=1`` means highest-priority slot in
      the operator's tenant). ``None`` when ``included=False``.
    * ``token_count`` -- the convention body's own estimated token
      cost via :func:`estimate_tokens`. Useful for the operator to
      see why a near-budget body got dropped (it weighed more than
      the remaining headroom).
    * ``would_drop_slugs`` -- the full ``dropped_slugs`` list the
      packer produced on this assembly. When ``included=True`` this
      names *other* slugs that fell out of the preamble (the just-
      written convention may have pushed a lower-priority neighbour
      out); when ``included=False`` the list contains *this* slug
      plus any others dropped under the same pack. The shape lets
      a single ``meho conventions create ...`` round-trip surface
      both the personal outcome (did mine land?) and the collateral
      damage (did mine push someone else out?).

    Why a separate model instead of inlining fields on
    :class:`Convention`: the response shape ``Convention`` is used
    by ``GET /{slug}`` too, where ``preamble_status`` would be
    redundant (operators inspecting a row already have the
    ``GET /api/v1/conventions``'s ``budget_status`` envelope for
    aggregate budget signal). Keeping the inclusion shape on a
    sub-model means GET-single can drop the field cleanly while
    POST/PATCH attach it.
    """

    model_config = ConfigDict(frozen=True)

    included: bool
    position: int | None = Field(default=None, ge=1)
    token_count: int = Field(ge=0)
    would_drop_slugs: list[str]


class Convention(BaseModel):
    """Full-row representation returned by GET-single / POST / PATCH.

    Carries the entire ``body`` text -- the natural shape for
    ``GET /{slug}`` and the return value of POST/PATCH where the
    caller wants to confirm what they wrote landed verbatim.
    ``kind`` is exposed as the raw string from the DB rather than
    as :class:`ConventionKind`; the API layer's pydantic models
    bound writes to the enum but read paths surface whatever the
    DB stored (a future ``kind`` value introduced by a migration
    would otherwise blow up the JSON encoder on every read).

    ``preamble_status`` (G0.14-T8 #1149) is the post-write inclusion
    signal POST/PATCH attach: ``None`` on ``GET /{slug}`` (the
    aggregate budget signal lives on the list response's
    ``budget_status``) and on writes against ``workflow`` /
    ``reference`` kinds (those are not preamble-bound). Populated
    only when the write touched an ``operational`` row.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    slug: str
    title: str
    body: str
    kind: str
    priority: int
    created_by_sub: str | None
    created_at: datetime
    updated_at: datetime
    preamble_status: PreambleInclusion | None = None


class BudgetStatus(BaseModel):
    """Preamble budget status for the operator's tenant.

    Surfaced as a sub-document on every ``GET /api/v1/conventions``
    call so the CLI's ``meho conventions list`` (T7 #1094) can warn
    when the tenant's ``kind='operational'`` set overflows the
    preamble budget and name the slugs that will be dropped from the
    agent session preamble. Without this surface, T3 #315's
    "lowest-priority slugs that will be dropped" acceptance
    criterion is unsatisfiable from a single list call -- the
    deferred AC the issue body folds back in.

    All four fields are populated by a single call to
    :func:`~meho_backplane.conventions.preamble.assemble_preamble`
    against the operator's tenant, so the budget arithmetic is
    end-to-end consistent with T4's preamble assembler (the same
    helper that produces the MCP ``initialize`` ``instructions``
    field). A divergence between the list surface's
    ``estimated_tokens`` and the preamble actually delivered to
    agent sessions would be a silent contract drift; sharing the
    primitive eliminates it by construction.

    Fields:

    * ``max_tokens`` -- the budget the preamble assembler enforces.
      Currently :data:`DEFAULT_MAX_PREAMBLE_TOKENS` (a module-level
      constant; configurable via the assembler's ``max_tokens``
      parameter in tests). Exposed on the list response so
      operators can do the budget math without grepping the source.
    * ``estimated_tokens`` -- :func:`estimate_tokens` over the
      assembled preamble text (header + guard + delimited kept
      blocks). Empty tenant: 0. Fitting tenant: positive integer
      below ``max_tokens``. Over-budget tenant: positive integer
      below ``max_tokens`` (the packer dropped slugs whole until it
      fit), but ``over_budget=True`` and ``dropped_slugs`` is
      non-empty.
    * ``over_budget`` -- convenience flag derived from
      ``len(dropped_slugs) > 0``. Cheap derived value but worth
      surfacing as a boolean so CLI / dashboard consumers branch on
      one bit rather than a length-of-list check.
    * ``dropped_slugs`` -- slugs that did not fit, in the packer's
      drop order (lowest-priority-first, ties broken by oldest-
      first). The CLI prints these on stderr with an
      "insufficient_budget" exit-code-5 warning so the operator
      knows exactly which conventions never reach an agent
      session.
    """

    model_config = ConfigDict(frozen=True)

    max_tokens: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    over_budget: bool
    dropped_slugs: list[str]


class ConventionListResponse(BaseModel):
    """Unified list envelope for ``GET /api/v1/conventions``.

    The `{items, next_cursor, ...sidecars}` shape codified in
    ``docs/codebase/api-shape-conventions.md`` §2. ``items`` carries the
    :class:`ConventionSummary` rows; ``next_cursor`` is always ``None``
    (the listing is not cursor-paginated) but present so the endpoint
    can grow pagination later without a further breaking change.

    ``budget_status`` (T7 #1094) is the §2 top-level *sidecar*: it
    carries the preamble budget arithmetic for the operator's tenant.
    Always populated; the underlying
    :func:`~meho_backplane.conventions.preamble.assemble_preamble` call
    is one indexed SELECT + an in-memory pack -- cheap enough to run on
    every list request. Exposing it on the list response (rather than on
    a separate ``/api/v1/conventions/budget-status`` route the issue
    body explicitly rejects) keeps the CLI / dashboard consumer paths to
    one HTTP round-trip.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ConventionSummary]
    next_cursor: str | None = None
    budget_status: BudgetStatus


class ConventionHistoryEntry(BaseModel):
    """One row from ``GET /api/v1/conventions/{slug}/history``.

    Per the issue's acceptance criterion, history returns newest
    first ("pick newest first for v0.2; documented"); the route's
    ORDER BY ``ts DESC`` enforces this. ``audit_id`` is the soft-FK
    that G8's audit-query path joins on; it is nullable because
    T5's seed migration inserts history rows with no audit_log row
    (seeded rows pre-date any HTTP request).

    ``body_before`` is nullable -- the first history row (the
    CREATE event) has no prior state. DELETE history rows get
    ``body_after=<final body>`` (a legible last-known state for
    audit forensics) rather than an empty-string sentinel; the
    lifecycle distinction (create / update / delete) lives in the
    audit log row's ``method`` / ``path`` columns, not on the
    history row itself.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    convention_id: uuid.UUID
    body_before: str | None
    body_after: str
    actor_sub: str
    ts: datetime
    audit_id: uuid.UUID | None
