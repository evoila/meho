# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parametric audit-log seed helper for G8.1-T5 acceptance tests (#469).

The G8.1 audit-query suite's correctness depends on the substrate
seeing a controlled corpus — tenant boundaries, op-id glob patterns,
operator-breadth distributions, and the cursor ordering must all be
deterministic from the test seed alone, not from whatever the audit
middleware would have written under load.

This module ships one orchestration helper plus a two-tenant overlap
shape the boundary scenarios share:

* :func:`seed_audit_row` — write one ``audit_log`` row with explicit
  ``occurred_at`` / ``id`` / target / op-id / op-class / payload
  control. The audit middleware is bypassed entirely; we go through
  the ORM directly so the test's seed sees identical row state across
  PG / SQLite engines.
* :func:`seed_audit_rows` — multi-row variant. Returns the inserted
  ``(occurred_at, id)`` pairs in insertion order so the caller can
  build cursor assertions against the same shape the substrate
  produces.
* :func:`seed_tenants_with_overlap` — the load-bearing two-tenant
  shape: tenant A and tenant B each get a row for the same target
  name (``"rdc-vcenter"``) and the same principal sub (``"damir"``).
  The substrate's tenant-boundary invariant is the assertion that
  tenant A's query never returns tenant B's row even when the names
  collide.

Why ORM-direct rather than HTTP-driven:

The chassis :class:`~meho_backplane.audit.AuditMiddleware` writes
exactly one row per authenticated request and pulls ``occurred_at``
from server-side ``datetime.now(UTC)``. The cursor-pagination
scenario needs 250 rows with controlled timestamps; running 250
authenticated POSTs would (a) flood the broadcast Valkey stream
with side-effect events, (b) take ~10 s per test even on a hot pool,
and (c) make the ``occurred_at`` clock non-deterministic enough to
break the ``LIMIT N+1`` next-cursor invariant. The integration suite
already verifies the middleware's write path
(``tests/test_audit_middleware.py``); the acceptance suite verifies
the *read* path against a known corpus.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog, Target

__all__ = [
    "seed_audit_row",
    "seed_audit_rows",
    "seed_target",
    "seed_tenants_with_overlap",
]


async def seed_target(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    product: str = "vcenter",
    host: str | None = None,
) -> uuid.UUID:
    """Insert one ``targets`` row and return its primary key.

    The substrate's ``target`` filter resolves via a ``LEFT JOIN
    targets ON tenant_id`` clause (see
    :func:`meho_backplane.audit_query.query.query_audit`), so a
    ``target_id`` on an audit row only surfaces ``target_name`` when
    the referenced target lives in the same tenant. Tests that
    exercise the join helper seed targets explicitly so the assertion
    on ``AuditEntry.target_name`` is grounded in real schema state.
    """
    target_id = uuid.uuid4()
    session.add(
        Target(
            id=target_id,
            tenant_id=tenant_id,
            name=name,
            product=product,
            host=host or f"{name}.example",
        )
    )
    await session.flush()
    return target_id


async def seed_audit_row(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    occurred_at: datetime,
    principal_sub: str,
    op_id: str,
    op_class: str,
    target_id: uuid.UUID | None = None,
    method: str = "POST",
    path: str = "/api/v1/x",
    status_code: int = 200,
    payload: dict[str, Any] | None = None,
    audit_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one fully-specified ``audit_log`` row.

    ``payload`` defaults to ``{"op_id": op_id, "op_class": op_class}``
    so the substrate's payload-side ``op_id`` / ``op_class`` predicate
    matches against the same identity the row's natural HTTP-shape
    would. The MCP-write path stores both keys explicitly inside
    ``payload`` (see ``mcp/handlers.py:214-221``); tests that exercise
    the substrate's OR-shaped predicate via a non-HTTP op-id need
    them present.

    Returns the inserted ``audit_log.id`` so the caller can build
    cursor / show-by-id assertions against a known row.
    """
    row_id = audit_id if audit_id is not None else uuid.uuid4()
    resolved_payload: dict[str, Any] = (
        dict(payload) if payload is not None else {"op_id": op_id, "op_class": op_class}
    )
    session.add(
        AuditLog(
            id=row_id,
            occurred_at=occurred_at,
            operator_sub=principal_sub,
            method=method,
            path=path,
            status_code=status_code,
            request_id=None,
            duration_ms=Decimal("1.0"),
            payload=resolved_payload,
            tenant_id=tenant_id,
            target_id=target_id,
        )
    )
    await session.flush()
    return row_id


async def seed_audit_rows(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    count: int,
    principal_sub: str = "damir",
    op_id: str = "vsphere.vm.list",
    op_class: str = "read",
    target_id: uuid.UUID | None = None,
    base_ts: datetime | None = None,
    spacing: timedelta = timedelta(seconds=1),
    status_code: int = 200,
) -> list[tuple[datetime, uuid.UUID]]:
    """Insert *count* audit rows with monotonically-increasing timestamps.

    The substrate orders ``(occurred_at DESC, id DESC)`` so the
    returned list — ``[(ts, id), ...]`` in insertion order, ascending
    timestamps — reads in the **opposite** order from the substrate's
    page output. Tests that expect "row N is first on page 1" use
    ``list(reversed(seeded))`` to project into the substrate's order.

    *spacing* of 1 s by default keeps the cursor's ``(ts, id)`` lex
    key strictly increasing — no two seeded rows share a timestamp,
    so the cursor's secondary ``id`` ordering is only exercised by
    the substrate when concurrent inserts collide in the same
    millisecond.
    """
    if base_ts is None:
        base_ts = datetime.now(UTC) - timedelta(seconds=count)
    seeded: list[tuple[datetime, uuid.UUID]] = []
    for idx in range(count):
        ts = base_ts + (spacing * idx)
        row_id = await seed_audit_row(
            session,
            tenant_id=tenant_id,
            occurred_at=ts,
            principal_sub=principal_sub,
            op_id=op_id,
            op_class=op_class,
            target_id=target_id,
            status_code=status_code,
        )
        seeded.append((ts, row_id))
    return seeded


async def seed_tenants_with_overlap(
    session: AsyncSession,
    *,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Seed two tenants with overlapping target names + operator subs.

    Both tenants get a ``"rdc-vcenter"`` target and a ``"damir"``
    operator. The substrate's tenant boundary must split them
    correctly: tenant-A's query for ``target="rdc-vcenter"`` returns
    only tenant-A's row, never tenant-B's, even though the name
    collides.

    Returns a dict keyed by ``"row_a"`` / ``"row_b"`` / ``"target_a"``
    / ``"target_b"`` for the test's downstream identity assertions.
    """
    now = datetime.now(UTC)
    target_a = await seed_target(
        session,
        tenant_id=tenant_a,
        name="rdc-vcenter",
        product="vcenter",
        host="vc-a.example",
    )
    target_b = await seed_target(
        session,
        tenant_id=tenant_b,
        name="rdc-vcenter",
        product="vcenter",
        host="vc-b.example",
    )
    row_a = await seed_audit_row(
        session,
        tenant_id=tenant_a,
        occurred_at=now - timedelta(seconds=60),
        principal_sub="damir",
        op_id="vsphere.vm.list",
        op_class="read",
        target_id=target_a,
    )
    row_b = await seed_audit_row(
        session,
        tenant_id=tenant_b,
        occurred_at=now - timedelta(seconds=30),
        principal_sub="damir",
        op_id="vsphere.vm.list",
        op_class="read",
        target_id=target_b,
    )
    return {
        "row_a": row_a,
        "row_b": row_b,
        "target_a": target_a,
        "target_b": target_b,
    }
