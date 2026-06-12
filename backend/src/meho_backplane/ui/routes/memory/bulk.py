# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Expiry visualisation + bulk select/delete/extend for the memory UI.

Initiative #341 (G10.4 Memory UI), Task #879 (T3). Pulled out of
:mod:`~meho_backplane.ui.routes.memory.views` so the T1 render module
stays at its existing size and the T3 additions live in one cohesive
unit:

* **Countdown formatting** -- :func:`format_countdown` renders a
  human-readable "expires in 3d 4h" string from a future ``expires_at``
  datetime, mirroring the ``meho status``-style cadence the rest of the
  console uses.
* **Expiry partition** -- :func:`partition_expired` splits a flat list
  of :class:`MemoryEntry` into ``(active, recently_expired)`` so the
  list template can render the two sections separately. The "recently
  expired" bucket is naturally bounded by the G5.2 sweeper window
  (#623) -- between expiry and the next sweeper tick, expired rows are
  still in the documents table and visible to the read path.
* **Bulk action dispatch** -- :func:`apply_bulk_action` looks up
  memories by their ``Document.id`` (tenant-scoped + ``source='memory'``
  filtered), gates each candidate against
  :class:`MemoryRbacResolver.can_write`, and routes to
  :meth:`MemoryService.forget` (delete) or
  :meth:`MemoryService.remember` (extend, body unchanged with a fresh
  ``expires_at``).

The bulk handler accepts memory ``Document.id`` UUIDs (the same value
the list template renders as ``data-memory-id`` on each card) rather
than the natural ``(scope, slug, target_name)`` triple. Reasons:

* IDs are the value the issue body specifies ("collects checked IDs").
* IDs are opaque -- a non-writable selection is rejected the same way
  whether the operator typed a UUID by hand or checked a box. Sending
  a triple leaks the existence of a scope/slug the operator can read
  but not write.
* The natural-key triple needs target_name on the wire for
  target-scoped rows; passing only UUIDs keeps the form body simple
  and avoids encoding/decoding edge cases.

The cost is one extra DB read per bulk action to resolve IDs to
natural keys; this is acceptable at UI rates (bulk actions are
operator-driven, not background traffic) and the same query that
re-renders the list runs after.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import structlog
from fastapi import HTTPException
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.memory import (
    MemoryEntry,
    MemoryRbacResolver,
    MemoryService,
    PermissionDeniedError,
)
from meho_backplane.memory._internal import MEMORY_SOURCE, document_to_entry

__all__ = [
    "BULK_ACTIONS",
    "BULK_EXTEND_DURATIONS",
    "BULK_MAX_IDS",
    "BulkActionResult",
    "apply_bulk_action",
    "format_countdown",
    "parse_bulk_ids",
    "parse_extend_duration",
    "partition_expired",
]

_log = structlog.get_logger(__name__)

#: Allowed values for the ``action`` form field on ``POST /ui/memory/bulk``.
#: Centralised here so the route handler, the template's button set, and
#: the service-layer dispatch agree on the literal strings.
BulkAction = Literal["delete", "extend"]
BULK_ACTIONS: Final[tuple[str, ...]] = ("delete", "extend")

#: Allowed values for the ``extend_duration`` form field. Pre-canned
#: options keep the UI honest -- an operator either bumps by a day or
#: by a week; arbitrary durations would invite "extend by 10 years"
#: footguns that defeat the TTL contract. The string maps to a
#: :class:`timedelta` in :data:`_EXTEND_DURATION_MAP`.
ExtendDuration = Literal["1d", "7d", "30d"]
BULK_EXTEND_DURATIONS: Final[tuple[str, ...]] = ("1d", "7d", "30d")

#: Upper bound on the number of memory IDs accepted in a single bulk
#: form post. Matches the list-view's :data:`LIST_LIMIT` cap so the
#: operator cannot select more rows than the list ever rendered.
#: Sending more than this on the wire is treated as malformed input
#: (likely a script abusing the route).
BULK_MAX_IDS: Final[int] = 500

_EXTEND_DURATION_MAP: Final[dict[str, timedelta]] = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@dataclass(frozen=True)
class BulkActionResult:
    """Outcome of one bulk action invocation.

    Returned by :func:`apply_bulk_action` and surfaced as a flash banner
    on the re-rendered list. Distinguishes between "the operator asked
    to act on 5 rows, 3 went through, 2 were RBAC-denied" and the
    all-or-nothing failure shape so the UX banner can be honest.
    """

    action: BulkAction
    requested: int
    succeeded: int
    denied: int
    missing: int

    @property
    def flash_message(self) -> str:
        """Build the flash banner the list template renders post-bulk."""
        verb = "deleted" if self.action == "delete" else "extended"
        parts = [f"{self.succeeded} {verb}"]
        if self.denied:
            parts.append(f"{self.denied} denied (RBAC)")
        if self.missing:
            parts.append(f"{self.missing} not found")
        return f"Bulk: {', '.join(parts)}."


# ---------------------------------------------------------------------------
# Countdown rendering
# ---------------------------------------------------------------------------


def format_countdown(expires_at: datetime, *, now: datetime | None = None) -> str:
    """Render a coarse human-readable countdown string.

    The operator wants "expires in 3d 4h" not "expires in 3 days,
    4 hours, 17 minutes, 22 seconds". Truncation rules:

    * ``>= 1 day`` -> ``"expires in {d}d {h}h"`` (drops minutes/seconds).
    * ``>= 1 hour`` -> ``"expires in {h}h {m}m"`` (drops seconds).
    * ``> 0`` -> ``"expires in {m}m"`` (drops seconds; clamped to 1m).
    * ``<= 0`` -> ``"expired"``. Callers should partition before
      calling so this branch only fires on rare timing skew at the
      partition boundary.

    The ``now`` parameter is exposed for unit tests so the truncation
    behaviour is pin-able without freezing the clock at module scope.
    """
    anchor = now if now is not None else datetime.now(UTC)
    delta = expires_at - anchor
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "expired"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"expires in {days}d {hours}h"
    if hours > 0:
        return f"expires in {hours}h {minutes}m"
    # ``minutes`` can be 0 when the delta is < 60s; show "1m" rather
    # than "0m" so the cue stays meaningful at the edge.
    return f"expires in {max(minutes, 1)}m"


# ---------------------------------------------------------------------------
# Partition (active vs recently expired)
# ---------------------------------------------------------------------------


def partition_expired(
    entries: Iterable[MemoryEntry],
    *,
    now: datetime | None = None,
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """Split *entries* into ``(active, recently_expired)``.

    "Recently expired" means ``expires_at <= now`` -- the row is past
    its TTL but still in the documents table because the G5.2 sweeper
    (#623) hasn't reaped it yet. The natural window is ``<=
    memory_expiry_tick_interval_seconds`` (24 h by default); callers
    don't need to apply a separate lower bound because the sweeper's
    next tick removes rows older than that.

    Entries with ``expires_at is None`` are always "active" (persistent
    memories, no TTL). The ``now`` parameter is exposed for tests.
    """
    anchor = now if now is not None else datetime.now(UTC)
    active: list[MemoryEntry] = []
    expired: list[MemoryEntry] = []
    for entry in entries:
        if entry.expires_at is not None and _is_expired_at(entry.expires_at, now=anchor):
            expired.append(entry)
        else:
            active.append(entry)
    return active, expired


def _is_expired_at(expires_at: datetime, *, now: datetime) -> bool:
    """Local mirror of :func:`~meho_backplane.memory._internal.is_expired`
    that accepts an explicit anchor.

    The shared :func:`~meho_backplane.memory._internal.is_expired` pins
    ``datetime.now(UTC)`` internally, which is correct for service
    callers but defeats unit-test pinning at the partition boundary.
    This helper preserves the ``<=`` semantics (a row landing at
    exactly ``now`` is treated as expired) so both anchors agree.
    """
    return expires_at <= now


# ---------------------------------------------------------------------------
# Bulk action parsing
# ---------------------------------------------------------------------------


def parse_bulk_ids(raw_ids: list[str]) -> list[uuid.UUID]:
    """Validate + parse the form's ``ids`` field into typed UUIDs.

    The form field arrives as ``list[str]`` (FastAPI's
    :class:`Form` with a list type for multi-value form fields).
    Validation:

    * Empty list -> :class:`HTTPException` 422. A bulk submit with no
      selection is operator error worth surfacing rather than a
      no-op.
    * Length above :data:`BULK_MAX_IDS` -> 422. Caps the worst-case
      DB round-trip per request.
    * Any non-UUID value -> 422 with the offending value in the
      detail (the operator's form was tampered with or HTMX
      mis-serialised the input).
    * Duplicates are de-duplicated silently -- the worst case is the
      operator clicked the same row twice; preserving the
      multiplicity would just cause two failed natural-key lookups
      on the second hit.
    """
    if not raw_ids:
        raise HTTPException(status_code=422, detail="bulk_no_ids_selected")
    if len(raw_ids) > BULK_MAX_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"bulk_too_many_ids (max {BULK_MAX_IDS})",
        )
    parsed: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in raw_ids:
        try:
            parsed_id = uuid.UUID(raw)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"bulk_invalid_id: {raw!r}",
            ) from exc
        if parsed_id in seen:
            continue
        seen.add(parsed_id)
        parsed.append(parsed_id)
    return parsed


def parse_extend_duration(raw: str | None) -> timedelta:
    """Resolve the ``extend_duration`` form value to a timedelta.

    ``None`` -> 422. The form's hidden default is ``"7d"`` so a missing
    value indicates form tampering; the route surfaces this as 422
    rather than picking a default the operator didn't ask for.
    """
    if raw is None:
        raise HTTPException(status_code=422, detail="bulk_missing_extend_duration")
    if raw not in _EXTEND_DURATION_MAP:
        raise HTTPException(
            status_code=422,
            detail=f"bulk_invalid_extend_duration: {raw!r} (allowed: {BULK_EXTEND_DURATIONS})",
        )
    return _EXTEND_DURATION_MAP[raw]


# ---------------------------------------------------------------------------
# Bulk action dispatch
# ---------------------------------------------------------------------------


async def _fetch_entries_by_ids(
    operator: Operator,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, MemoryEntry]:
    """Resolve *ids* to :class:`MemoryEntry` rows in *operator*'s tenant.

    Tenant-scoped + ``source='memory'`` filtered so an operator cannot
    smuggle a non-memory document ID (a knowledge-base row, an audit
    row) into the bulk form and have the route mutate it. Returns a
    dict keyed by ID so callers can report per-ID outcomes without a
    second lookup; IDs absent from the dict are the "missing" bucket.
    """
    if not ids:
        return {}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(Document).where(
                Document.tenant_id == operator.tenant_id,
                Document.source == MEMORY_SOURCE,
                Document.id.in_(ids),
            )
        )
        docs = list(result.scalars().all())
    return {doc.id: document_to_entry(doc) for doc in docs}


async def _bulk_delete_one(
    service: MemoryService,
    operator: Operator,
    entry: MemoryEntry,
) -> bool:
    """Delete one entry. Returns ``True`` on success.

    Returns ``False`` on :class:`PermissionDeniedError` so the outer
    loop can tally a denied count without halting the batch. Any other
    exception (:class:`ValueError` from a malformed target row, a DB
    failure) propagates -- a bulk action that hits an unexpected
    failure is louder if surfaced than swallowed.
    """
    try:
        await service.forget(
            operator=operator,
            scope=entry.scope,
            slug=entry.slug,
            target_name=entry.target_name,
        )
    except PermissionDeniedError:
        return False
    return True


async def _bulk_extend_one(
    service: MemoryService,
    operator: Operator,
    entry: MemoryEntry,
    new_expires_at: datetime,
) -> bool:
    """Re-write *entry* with a fresh ``expires_at``.

    Uses :meth:`MemoryService.remember` with the same body + metadata
    so the natural key is preserved and only ``metadata.expires_at``
    changes. The service strips the bookkeeping keys before merging
    so we don't double-write them.
    """
    metadata = {
        k: v
        for k, v in entry.metadata.items()
        if k not in {"scope", "user_sub", "target_name", "expires_at"}
    }
    try:
        await service.remember(
            operator=operator,
            scope=entry.scope,
            body=entry.body,
            slug=entry.slug,
            metadata=metadata,
            expires_at=new_expires_at,
            target_name=entry.target_name,
        )
    except PermissionDeniedError:
        return False
    return True


async def apply_bulk_action(
    operator: Operator,
    *,
    action: BulkAction,
    ids: list[uuid.UUID],
    extend_duration: timedelta | None = None,
    now: datetime | None = None,
) -> BulkActionResult:
    """Dispatch one bulk action across *ids*.

    Resolution + RBAC posture:

    1. **Tenant filter** -- ``_fetch_entries_by_ids`` only returns rows
       whose ``tenant_id == operator.tenant_id`` AND
       ``source='memory'``. Rows the operator's tenant cannot see (or
       isn't a memory row) are silently missing -- counted under
       ``missing`` so the flash banner reports them, but never acted
       on.
    2. **Per-row RBAC re-check** -- :class:`MemoryRbacResolver` is
       consulted on each row. The service's ``forget`` / ``remember``
       call also re-checks; the route-side check filters the bulk into
       a writable subset *before* the service mutates so the count is
       honest and a denied row doesn't half-act (the service is
       idempotent on denial, but the count would be wrong without
       this pass).
    3. **Per-row dispatch** -- ``delete`` -> ``forget``; ``extend`` ->
       ``remember`` with the existing body + a fresh ``expires_at``.

    The ``now`` parameter is exposed for tests so the extend anchor is
    pin-able.
    """
    if action not in BULK_ACTIONS:
        # Defensive -- the route's Literal-typed Form should reject
        # this before we get here, but a direct caller could pass an
        # arbitrary string.
        raise HTTPException(status_code=422, detail=f"bulk_invalid_action: {action!r}")
    if action == "extend" and extend_duration is None:
        raise HTTPException(status_code=422, detail="bulk_extend_requires_duration")

    entries_by_id = await _fetch_entries_by_ids(operator, ids)
    missing = len(ids) - len(entries_by_id)

    rbac = MemoryRbacResolver()
    service = MemoryService(rbac=rbac)
    anchor = now if now is not None else datetime.now(UTC)
    new_expires_at = anchor + extend_duration if extend_duration else None

    succeeded = 0
    denied = 0
    for entry in entries_by_id.values():
        if not rbac.can_write(operator, entry.scope, entry.target_name):
            denied += 1
            continue
        if action == "delete":
            ok = await _bulk_delete_one(service, operator, entry)
        else:
            # ``new_expires_at`` is non-None on the extend branch
            # (validated above); the assertion silences mypy.
            assert new_expires_at is not None
            ok = await _bulk_extend_one(service, operator, entry, new_expires_at)
        if ok:
            succeeded += 1
        else:
            # Service-layer permission denial -- the route-side check
            # already passed, so this is the rare "RBAC changed between
            # check and act" race. Count under denied so the banner
            # reports it; the row stayed intact.
            denied += 1

    result = BulkActionResult(
        action=action,
        requested=len(ids),
        succeeded=succeeded,
        denied=denied,
        missing=missing,
    )
    _log.info(
        "ui_memory_bulk",
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        action=action,
        requested=result.requested,
        succeeded=result.succeeded,
        denied=result.denied,
        missing=result.missing,
    )
    return result
