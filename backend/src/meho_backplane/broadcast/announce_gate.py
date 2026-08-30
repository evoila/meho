# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Opt-in dispatch-time announce gate (#3133, Initiative #3128).

The enforcing companion to the reflex advisory
(:mod:`meho_backplane.broadcast.reflex`). Where the advisory *nudges*,
this gate -- **off by default, opt-in per tenant** -- *rejects* a
write-class operation of ``caution`` safety or higher, before execution,
when the caller holds no active announce claim covering it. The rejection
is a structured, fail-loud policy denial naming ``meho_broadcast_announce``
-- it is **not** a NEEDS_APPROVAL path (no durable approval row, no
four-eyes queue).

Composition boundary: #2546 rate-limits the announce *call* (anti-flood);
this gates *dispatch* on announce-*state*. The two compose -- a caller
announces (subject to the rate limit), then its write dispatches pass the
gate.

Tenant enablement is a **structured per-tenant policy field** --
``tenant.announce_gate_enabled`` (Boolean, default ``False``) -- read
through a cache-aware, fail-open resolver that mirrors the
``broadcast_override`` precedent
(:mod:`meho_backplane.broadcast.overrides`): a 60s per-tenant TTL cache
keeps the flag off the DB per-dispatch, and any read error resolves to
``False`` (gate disabled). It deliberately does **not** live in
``tenant_conventions`` (free-form Markdown, unsuitable for a machine-read
boolean policy). A typed column on the canonical per-tenant row is the
minimum structured store for a single boolean; a dedicated rules table +
CRUD/REST/MCP/UI surface (the full ``broadcast_override`` shape) is more
than a one-flag policy warrants -- opt-in tenants set the flag through
seeding / tenant administration, and a management verb is a clean
follow-up.

Fail-open throughout (the #2550 / #2718 advisory mould): a disabled or
unreadable policy, or an unreachable claim scan, never blocks a
dispatch -- the gate only ever *adds* a rejection when it can positively
determine the tenant opted in AND the claim is absent.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import classify_op
from meho_backplane.broadcast.history import (
    WRITE_OP_CLASSES,
    caller_has_active_announce_claim,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant

__all__ = [
    "announce_gate_blocks",
    "announce_gate_enabled",
    "reset_announce_gate_cache_for_testing",
]

_log = structlog.get_logger(__name__)

#: Ordinal rank of the closed ``safety_level`` vocabulary (the
#: ``ck_endpoint_descriptor_safety_level`` CHECK set). The gate applies at
#: ``caution`` and above; the string type carries no ordering of its own,
#: so this map is the single place the ``>= caution`` threshold is encoded.
#: An unknown level (never expected -- the column is CHECK-constrained)
#: ranks ``0`` so the gate stays permissive (fail-open) on it.
_SAFETY_RANK: Final[dict[str, int]] = {
    "safe": 0,
    "caution": 1,
    "dangerous": 2,
    "destructive": 3,
}
_SAFETY_GATE_THRESHOLD: Final[int] = _SAFETY_RANK["caution"]

#: Per-tenant enablement cache, mirroring ``overrides._TENANT_CACHE``.
#: Value = (enabled, monotonic_expires_at). A hit is a pure dict lookup;
#: only a miss awaits the one-row SELECT.
_CACHE_TTL_SECONDS: Final[float] = 60.0
_TENANT_GATE_CACHE: dict[UUID, tuple[bool, float]] = {}

#: The structured, fail-loud remediation. Names the exact meta-tool and
#: what to declare, in the remediation style the rest of dispatch uses.
_ANNOUNCE_REQUIRED_REMEDIATION: Final[str] = (
    "this tenant requires an active announce claim before a caution-or-higher "
    "write-class operation. Call meho_broadcast_announce declaring your intent "
    "(planned_op_class + target), then retry the operation."
)


def reset_announce_gate_cache_for_testing() -> None:
    """Clear the per-tenant enablement cache (test isolation only)."""
    _TENANT_GATE_CACHE.clear()


async def announce_gate_enabled(tenant_id: UUID) -> bool:
    """Whether the announce gate is enabled for *tenant_id* (default ``False``).

    Reads ``tenant.announce_gate_enabled`` through a 60s per-tenant TTL
    cache. Fail-open: a missing tenant row or any DB error resolves to
    ``False`` (gate disabled), warn-logged as
    ``announce_gate_enablement_read_failed`` -- an unreadable policy never
    blocks a dispatch.
    """
    now = time.monotonic()
    cached = _TENANT_GATE_CACHE.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(Tenant.announce_gate_enabled).where(Tenant.id == tenant_id)
            )
            enabled = bool(result.scalar_one_or_none())
    except Exception:
        _log.warning("announce_gate_enablement_read_failed", tenant_id=str(tenant_id))
        return False
    _TENANT_GATE_CACHE[tenant_id] = (enabled, now + _CACHE_TTL_SECONDS)
    return enabled


async def announce_gate_blocks(
    operator: Operator,
    *,
    op_id: str,
    safety_level: str,
    target_name: str | None,
) -> str | None:
    """Return a remediation string if the announce gate blocks this op, else ``None``.

    Short-circuits (returns ``None`` -- no block) in order, cheapest first:

    * the op is not write-class (:func:`classify_op` not in
      :data:`~meho_backplane.broadcast.history.WRITE_OP_CLASSES`) -- no
      policy read;
    * the op's ``safety_level`` is below ``caution`` -- no policy read;
    * the tenant has not enabled the gate (:func:`announce_gate_enabled`);
    * the caller holds an active covering claim
      (:func:`~meho_backplane.broadcast.history.caller_has_active_announce_claim`).

    Only when the tenant opted in AND the claim is absent does it return
    :data:`_ANNOUNCE_REQUIRED_REMEDIATION`. Fail-open: any internal error
    resolves to ``None`` (no block), warn-logged as
    ``announce_gate_check_failed``.
    """
    try:
        if classify_op(op_id) not in WRITE_OP_CLASSES:
            return None
        if _SAFETY_RANK.get(safety_level, 0) < _SAFETY_GATE_THRESHOLD:
            return None
        if not await announce_gate_enabled(operator.tenant_id):
            return None
        if await caller_has_active_announce_claim(operator, op_id=op_id, target_name=target_name):
            return None
        return _ANNOUNCE_REQUIRED_REMEDIATION
    except Exception:
        # Fail-open: the gate only ever ADDS a rejection when it can
        # positively determine the tenant opted in and the claim is
        # absent. An internal error must never convert a would-be
        # dispatch into a denial.
        _log.warning("announce_gate_check_failed", tenant_id=str(operator.tenant_id))
        return None
