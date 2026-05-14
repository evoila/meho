# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 models for the memory module.

G5.1-T1 (#421). The five :class:`MemoryScope` values mirror the
consumer-needs.md §G5 shape verbatim and double as the user-facing
identifier (URL path segment for T2's routes, scope arg for T3's MCP
tools, ``--scope`` flag for T4's CLI). The string value matches the
suffix of the underlying ``kind`` column in the ``documents`` table
(``memory-<scope>``) so audit rows and observability traces correlate
without a translation table.

:class:`MemoryEntry` is the read shape returned by every accessor on
:class:`~meho_backplane.memory.service.MemoryService` (``recall``,
``list_memories``); :class:`MemoryEntryCreate` is the write shape
:class:`~meho_backplane.memory.service.MemoryService.remember` accepts.
Both are frozen (``ConfigDict(frozen=True)``) so a caller can stash a
returned entry in a log record or audit row without fear of mutation;
:class:`MemoryEntrySearchHit` carries the retrieval-rank metadata
``search_memories`` surfaces.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SLUG_PATTERN",
    "MemoryEntry",
    "MemoryEntryCreate",
    "MemoryEntrySearchHit",
    "MemoryScope",
    "kind_for_scope",
    "scope_for_kind",
    "validate_slug",
]


#: Regex anchoring the slug character set the service accepts.
#: Constrains operator-supplied slugs to the safe-URL alphabet so the
#: ``source_id`` encoding scheme in :mod:`meho_backplane.memory._internal`
#: (colon-separated segments) stays uniquely decodable. Without this
#: constraint, an operator-supplied slug containing ``:`` would round-
#: trip asymmetrically: ``encode_source_id`` joins on ``:``,
#: ``slug_from_source_id`` reverses with ``rsplit(':', 1)``, so a slug
#: like ``"foo:bar"`` would be silently truncated to ``"bar"`` on
#: read. The pattern admits letters, digits, hyphen, underscore, and
#: dot -- enough for human-friendly identifiers (``wine-preference``,
#: ``k8s.rollout-note``) and machine-generated ones (the
#: :func:`~meho_backplane.memory._internal.auto_slug` hex prefix)
#: while excluding colon, slash, and whitespace.
SLUG_PATTERN: str = r"^[A-Za-z0-9_\-\.]+$"

#: Compiled form of :data:`SLUG_PATTERN`. Module-level so the
#: service-layer slug validator at write boundaries does not pay
#: per-call compile cost.
_SLUG_RE: re.Pattern[str] = re.compile(SLUG_PATTERN)


def validate_slug(slug: str) -> str:
    """Validate *slug* against :data:`SLUG_PATTERN` and return it on success.

    Raises :class:`ValueError` when *slug* contains characters outside
    the safe set. Service-layer write boundaries
    (:meth:`MemoryService.remember`, :meth:`MemoryService.forget`,
    :meth:`MemoryService.recall`) call this so direct (non-Pydantic)
    callers cannot smuggle a colon-containing slug past the
    :class:`MemoryEntryCreate` validator. Pure function; returns the
    input unchanged on success so call sites can use
    ``slug = validate_slug(slug)`` for fluent composition.
    """
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"slug {slug!r} contains characters outside the safe set "
            f"(allowed: letters, digits, hyphen, underscore, dot)"
        )
    return slug


class MemoryScope(StrEnum):
    """One of the five memory scopes from consumer-needs.md §G5 L137-141.

    The string value is the wire-level identifier (route path segment,
    CLI flag value, MCP tool arg). Each value maps to a single
    ``documents.kind`` row prefixed with ``memory-`` --
    :func:`kind_for_scope` is the canonical translation. The mapping is
    one-to-one and bijective with :func:`scope_for_kind`, so callers
    never round-trip through ad-hoc string-prefixing.

    ``StrEnum`` (PEP 663, stdlib in 3.11+) gives the members ``str``
    semantics for free: ``f"scope={MemoryScope.USER}"`` renders as
    ``"scope=user"`` rather than ``"scope=MemoryScope.USER"``, matching
    the :class:`~meho_backplane.auth.operator.TenantRole` convention.
    """

    USER = "user"
    USER_TENANT = "user-tenant"
    USER_TARGET = "user-target"
    TENANT = "tenant"
    TARGET = "target"


#: Scopes that require ``target_name`` at write time. The service
#: validates this at ``remember`` boundary so an invalid combination
#: never reaches the indexer.
TARGET_SCOPED: frozenset[MemoryScope] = frozenset({MemoryScope.USER_TARGET, MemoryScope.TARGET})

#: Scopes whose visibility is gated by ``user_sub`` -- the operator
#: that wrote the memory is the only one who can read it back. Used by
#: :class:`~meho_backplane.memory.rbac.MemoryRbacResolver.can_read`.
USER_SCOPED: frozenset[MemoryScope] = frozenset(
    {MemoryScope.USER, MemoryScope.USER_TENANT, MemoryScope.USER_TARGET}
)


def kind_for_scope(scope: MemoryScope) -> str:
    """Translate a scope to the ``documents.kind`` value the row carries.

    Pure mapping function so callers never derive ``"memory-" +
    scope.value`` inline -- the prefix lives here once and the test
    suite asserts the round-trip with :func:`scope_for_kind`. The
    return value is what ``index_document`` writes to ``kind`` and
    what ``retrieve`` filters on.
    """
    return f"memory-{scope.value}"


def scope_for_kind(kind: str) -> MemoryScope:
    """Inverse of :func:`kind_for_scope`. Raises on unknown ``kind``.

    Used when reading rows back from the ``documents`` table -- the
    service receives a Document with ``kind="memory-user-tenant"`` and
    needs the typed :class:`MemoryScope` for the returned
    :class:`MemoryEntry`. Raising on unknown kinds is the right
    failure mode: a memory-kind value the enum doesn't cover means
    either a corrupt write path or a forgotten enum extension, both of
    which should surface loudly.
    """
    if not kind.startswith("memory-"):
        raise ValueError(f"kind {kind!r} is not a memory kind")
    suffix = kind[len("memory-") :]
    return MemoryScope(suffix)


class MemoryEntry(BaseModel):
    """Read shape -- one memory row as the service returns it.

    Frozen so callers can stash the entry in audit / log records
    without mutation surprises. ``expires_at`` is lifted out of
    ``doc_metadata`` for filter convenience -- the read-side filter in
    :meth:`~meho_backplane.memory.service.MemoryService.list_memories`
    and :meth:`~meho_backplane.memory.service.MemoryService.recall`
    compares it without re-parsing the dict. The raw ``metadata`` is
    still carried so callers (audit middleware, MCP resource handlers)
    see the original payload.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    scope: MemoryScope
    slug: str
    body: str
    metadata: dict[str, Any]
    expires_at: datetime | None
    user_sub: str | None
    target_name: str | None
    created_at: datetime
    updated_at: datetime


class MemoryEntryCreate(BaseModel):
    """Write shape -- the inputs ``remember`` consumes.

    Carried as a separate model from :class:`MemoryEntry` so the API
    surface (T2 #422) can validate the request body via Pydantic
    without the read-only ``id`` / ``created_at`` / ``updated_at``
    fields the storage layer fills in. ``slug`` is optional --
    :meth:`~meho_backplane.memory.service.MemoryService.remember`
    auto-generates one when ``None``.
    """

    model_config = ConfigDict(frozen=True)

    scope: MemoryScope
    body: str = Field(min_length=1)
    # ``slug`` is constrained to the safe-URL alphabet so the colon
    # the ``source_id`` encoding uses as a segment separator can
    # never appear inside a slug. Without this guard, the round-trip
    # asymmetry between :func:`encode_source_id` (joins on ``:``) and
    # :func:`slug_from_source_id` (reverses with rsplit) would
    # silently truncate operator-supplied slugs containing ``:``.
    # Pydantic's :class:`Field` ``pattern`` runs at model
    # construction time; the service-layer :func:`validate_slug` is
    # the parallel gate for non-Pydantic call paths.
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    metadata: dict[str, Any] | None = None
    expires_at: datetime | None = None
    target_name: str | None = None


class MemoryEntrySearchHit(BaseModel):
    """One ranked hit from :meth:`MemoryService.search_memories`.

    Wraps the retrieval-substrate :class:`RetrievalHit` shape, exposing
    only the memory-relevant fields plus the fused / per-signal
    ranking metadata so callers can build "why was this surfaced" UX
    without re-running the query. Frozen for the same reason as
    :class:`MemoryEntry`.
    """

    model_config = ConfigDict(frozen=True)

    entry: MemoryEntry
    fused_score: float
    bm25_score: float | None
    cosine_score: float | None
    bm25_rank: int | None
    cosine_rank: int | None
