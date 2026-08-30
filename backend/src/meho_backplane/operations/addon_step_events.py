# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add-on step-event push contract — durable recorder + resumable read (#3027).

The step-event push contract (Initiative #2900, Task #3027) gives a paired
add-on a **durable, resumable** outbound subscription to the step events
that belong to its own work — approval outcomes and dispatch completions —
replacing the at-most-once, count-trimmed Valkey SSE feed a restart would
silently lose events across.

Two responsibilities, one cohesive service:

* **Record** (:meth:`AddonStepEventService.record_if_owned` /
  :meth:`record_if_owned_committed`) — called at the producer sites
  (approval-lifecycle notifications, agent-run terminal transition) with
  the responsible principal's Keycloak ``sub``. A step event is written
  **only** when that ``sub`` matches a pairing's
  :attr:`~meho_backplane.db.models.AddonPairing.service_account_sub`; the
  row is stamped with that ``pairing_id``. A produced event whose principal
  is not a paired add-on is a cheap no-op (one indexed lookup, no write),
  so the overwhelmingly common non-add-on producer path costs almost
  nothing.

* **Read** (:meth:`AddonStepEventService.list_for_pairing`) — the
  subscription surface. Bound (by the caller's token ``sub`` →
  :meth:`resolve_pairing_for_sub`) to the caller's **own** pairing, it
  returns that pairing's events with ``seq > after`` in ``seq`` order.
  ``seq`` is a monotonic ``BIGSERIAL`` cursor, so an add-on persists the
  last ``seq`` it saw, reconnects, and reads strictly forward — no missed
  events across its own restarts (durable delivery with resume).

The ``seq > after`` high-watermark cursor is only exact if, for a given
pairing, ``seq`` order equals **commit** order. It does not on its own:
``seq`` is drawn at INSERT (flush) but a row only becomes visible at
COMMIT, so two concurrent writes to the same pairing can commit out of
``seq`` order — a lower ``seq`` still uncommitted while a higher ``seq``
is already visible. A reader that advances its cursor past the higher
``seq`` would then permanently skip the lower one. The recorder closes
that gap by holding a **transaction-scoped per-pairing advisory lock**
(:func:`_pairing_seq_lock_key`) across the ``seq``-assign→commit window,
so the two windows cannot interleave and commit order equals ``seq``
order per pairing. The "no missed events" property is therefore
structural, not best-effort.

Scoping is structural: attribution happens at write time by identity, so a
pairing's log only ever contains that pairing's events. An event outside
the paired principal's lineage is never written into another pairing's log
and therefore can never be delivered — the property the acceptance test
pins.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, AddonStepEvent

__all__ = [
    "AddonStepEventService",
    "StepEventListResponse",
    "StepEventRead",
]

_log = structlog.get_logger(__name__)

#: Domain-separation prefix for the per-pairing seq-serialization advisory
#: key, so this key space never collides with another subsystem that hashes
#: a bare UUID into ``pg_advisory_xact_lock`` (the topology scheduler, the
#: checks dashboard-transition claim). A collision would only cost a
#: spurious wait, never a correctness bug, but the prefix keeps the key
#: spaces disjoint by construction.
_SEQ_LOCK_KEY_DOMAIN = b"addon_step_event_seq:"


def _pairing_seq_lock_key(pairing_id: uuid.UUID) -> int:
    """Map a pairing id to a stable signed-63-bit advisory-lock key.

    ``pg_advisory_xact_lock`` takes a ``bigint`` (signed 64-bit). A blake2b
    digest of the pairing UUID under a domain-separation prefix is
    deterministic and well-distributed; masking to 63 bits keeps it
    non-negative so it round-trips through asyncpg's ``bigint`` binding
    without tripping the sign bit. Mirrors
    :func:`meho_backplane.checks.investigate._dashboard_transition_lock_key`.
    """
    digest = hashlib.blake2b(_SEQ_LOCK_KEY_DOMAIN + pairing_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


class StepEventRead(BaseModel):
    """One step event as delivered to a subscribed add-on."""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    id: uuid.UUID
    event_kind: str
    work_ref: str | None
    audit_id: uuid.UUID | None
    payload: dict[str, object]
    created_at: datetime


class StepEventListResponse(BaseModel):
    """A page of step events plus the cursor to resume past it.

    ``next_cursor`` is the ``seq`` of the last returned event (as a string
    so the wire cursor is opaque and dialect-agnostic); ``None`` when the
    page is empty. The add-on passes it back as ``after`` to read strictly
    forward on the next poll / reconnect.
    """

    model_config = ConfigDict(frozen=True)

    items: list[StepEventRead]
    next_cursor: str | None = None


class AddonStepEventService:
    """Record + read durable step events for paired add-ons (#3027).

    Stateless; instantiate per request and call freely.
    """

    def __init__(self) -> None:
        self._log = _log

    async def _resolve_pairing_id(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        service_account_sub: str,
    ) -> uuid.UUID | None:
        """Return the pairing id whose service-account ``sub`` matches, else ``None``.

        The ``addon_pairing_service_account_sub_idx`` makes this a cheap
        indexed lookup. A ``NULL`` ``service_account_sub`` (a pre-#3027
        pairing) never matches a real ``sub``, so an un-backfilled pairing
        fails closed until it re-pairs.
        """
        result = await session.execute(
            select(AddonPairing.id).where(
                AddonPairing.tenant_id == tenant_id,
                AddonPairing.service_account_sub == service_account_sub,
            )
        )
        return result.scalar_one_or_none()

    async def _serialize_pairing_seq(self, session: AsyncSession, pairing_id: uuid.UUID) -> None:
        """Serialize the ``seq``-assign→commit window for *pairing_id* on PG.

        The durable-resume guarantee (AC1) is that a reader resuming from
        ``seq > after`` never skips a **committed** lower ``seq``. ``seq``
        is a ``BIGSERIAL`` drawn at INSERT (flush) but only made visible at
        COMMIT, so without serialization two writes to the same pairing can
        commit out of ``seq`` order: writer A draws ``seq=N`` and commits
        slowly while writer B draws ``seq=N+1`` and commits first. A reader
        that advances its cursor to ``N+1`` before A commits then never
        returns ``N`` — a permanently skipped event.

        A transaction-scoped per-pairing advisory lock, taken **before** the
        INSERT and held to the caller's commit (``pg_advisory_xact_lock``
        releases only at COMMIT / ROLLBACK), forces one writer's whole
        [draw ``seq`` … commit] window to complete before the next writer to
        the same pairing can draw its ``seq``. For a given pairing, ``seq``
        order therefore equals commit order, so when a committed row with
        ``seq=M`` is visible every ``seq < M`` for that pairing has already
        committed — the high-watermark cursor is exact.

        Scope is per pairing, so the lock never contends across pairings, and
        the per-pairing write rate (one add-on's own approval outcomes +
        dispatch completions) is low, so the serialization cost is
        negligible. A no-op on non-PostgreSQL dialects: the SQLite unit-test
        path is single-writer, so ``seq`` order already equals commit order
        there. Mirrors
        :func:`meho_backplane.checks.investigate._lock_dashboard_transition`.
        """
        conn = await session.connection()
        if conn.dialect.name != "postgresql":
            return
        key = _pairing_seq_lock_key(pairing_id)
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

    async def record_if_owned(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        owner_principal_sub: str | None,
        event_kind: str,
        work_ref: str | None,
        audit_id: uuid.UUID | None,
        payload: dict[str, object],
    ) -> AddonStepEvent | None:
        """Append a step event in *session* iff its owner is a paired add-on.

        Same-transaction discipline: the row is added and flushed (so
        ``seq`` is populated) but **not** committed — the caller's
        transaction owns the commit, so a producer rollback discards the
        step event alongside the state change that produced it.

        Before the INSERT draws ``seq``, the caller's transaction takes the
        per-pairing advisory lock (:meth:`_serialize_pairing_seq`) held to
        commit, so concurrent writes to the same pairing commit in ``seq``
        order and the resume cursor can never skip a committed lower ``seq``.
        The lock is taken only once the owner is known to be a paired add-on,
        so the common non-add-on producer path never touches it.

        Returns the inserted :class:`AddonStepEvent`, or ``None`` when
        *owner_principal_sub* is absent or not a paired add-on's
        service-account ``sub`` (the cheap no-op common path).
        """
        if not owner_principal_sub:
            return None
        pairing_id = await self._resolve_pairing_id(
            session,
            tenant_id=tenant_id,
            service_account_sub=owner_principal_sub,
        )
        if pairing_id is None:
            return None
        await self._serialize_pairing_seq(session, pairing_id)
        row = AddonStepEvent(
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            event_kind=event_kind,
            work_ref=work_ref,
            audit_id=audit_id,
            payload=payload,
        )
        session.add(row)
        await session.flush()
        return row

    async def record_if_owned_committed(
        self,
        *,
        tenant_id: uuid.UUID,
        owner_principal_sub: str | None,
        event_kind: str,
        work_ref: str | None,
        audit_id: uuid.UUID | None,
        payload: dict[str, object],
    ) -> None:
        """Fail-open variant that owns its own committed transaction.

        For producer sites without an ambient session in the producing
        transaction — the post-commit approval-lifecycle notification. It
        opens its own session, records, and commits. A failure is logged
        and swallowed (fail-open), matching the surrounding approval
        broadcast posture: a step-event write must never block the durable
        approval decision, and the add-on re-reads forward by ``seq`` on
        its next poll.
        """
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                row = await self.record_if_owned(
                    session,
                    tenant_id=tenant_id,
                    owner_principal_sub=owner_principal_sub,
                    event_kind=event_kind,
                    work_ref=work_ref,
                    audit_id=audit_id,
                    payload=payload,
                )
                if row is None:
                    return
                await session.commit()
        except Exception:
            self._log.exception(
                "addon_step_event_record_failed",
                event_kind=event_kind,
                tenant_id=str(tenant_id),
            )

    async def resolve_pairing_for_sub(
        self,
        *,
        tenant_id: uuid.UUID,
        service_account_sub: str,
    ) -> AddonPairing | None:
        """Return the pairing the caller's token ``sub`` binds to, else ``None``.

        The subscription bind: a paired add-on authenticates as its
        service principal, whose ``sub`` is exactly the
        ``service_account_sub`` captured at pair time. A caller whose
        ``sub`` matches no pairing (a non-add-on service principal, or a
        pre-#3027 pairing with a ``NULL`` ``service_account_sub``) resolves
        to ``None`` and is refused.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AddonPairing).where(
                    AddonPairing.tenant_id == tenant_id,
                    AddonPairing.service_account_sub == service_account_sub,
                )
            )
            return result.scalar_one_or_none()

    async def list_for_pairing(
        self,
        *,
        pairing_id: uuid.UUID,
        after_seq: int = 0,
        limit: int = 100,
    ) -> StepEventListResponse:
        """Return this pairing's step events with ``seq > after_seq``, ``seq``-ordered.

        The durable resume read: ``after_seq`` is the last ``seq`` the
        add-on saw (``0`` on a cold start reads from the beginning of the
        retained log). Rows are ordered by the monotonic ``seq`` and
        capped at *limit*; ``next_cursor`` is the last returned ``seq`` so
        the add-on reads strictly forward on its next call.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1; got {limit}")
        if after_seq < 0:
            raise ValueError(f"after_seq must be >= 0; got {after_seq}")
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AddonStepEvent)
                .where(
                    AddonStepEvent.pairing_id == pairing_id,
                    AddonStepEvent.seq > after_seq,
                )
                .order_by(AddonStepEvent.seq)
                .limit(limit)
            )
            rows = result.scalars().all()
        items = [StepEventRead.model_validate(row) for row in rows]
        next_cursor = str(items[-1].seq) if items else None
        return StepEventListResponse(items=items, next_cursor=next_cursor)
