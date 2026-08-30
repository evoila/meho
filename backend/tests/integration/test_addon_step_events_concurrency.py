# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Concurrency coverage for the add-on step-event recorder (#3027, B1).

The unit suite (`tests/test_addon_step_events.py`) proves durable resume
for **serial** writes on SQLite, but SQLite is single-writer and cannot
stage the failure mode the resume cursor is actually exposed to: two
writes to the same pairing that commit out of `seq` order. `seq` is a
`BIGSERIAL` drawn at INSERT (flush) but only visible at COMMIT, so writer A
can draw the lower `seq`, hold it uncommitted, while writer B draws the
higher `seq` and commits first. A reader between the two commits would
advance its high-watermark cursor past B and then `seq > cursor`
permanently skips the committed A — a lost event.

`AddonStepEventService._serialize_pairing_seq` closes the gap with a
transaction-scoped per-pairing advisory lock across the assign→commit
window. These tests exercise it against the real `pgvector/pgvector:pg16`
container:

* **serialization** — writer A holds the lock with an uncommitted lower
  `seq`; writer B is forced to block at the lock (proven with an explicit
  "no committed event is visible yet" read), so no higher committed `seq`
  can exist ahead of the uncommitted lower one. After A commits, a cursor
  walk misses nothing.
* **scope** — the lock is *per pairing*: a concurrent write to a different
  pairing does not block behind A's lock, so the serialization never
  degrades into a global write bottleneck.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, AddonStepEvent
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.operations.addon_step_events import AddonStepEventService
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Matches the tenant row the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


async def _seed_pairing(*, name: str, service_account_sub: str) -> uuid.UUID:
    """Insert one paired add-on under tenant A and return its id."""
    pairing_id = uuid.uuid4()
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        session.add(
            AddonPairing(
                id=pairing_id,
                tenant_id=TENANT_A_ID,
                name=name,
                keycloak_client_id=f"addon:{name}",
                keycloak_internal_id=f"kc-{name}",
                service_account_sub=service_account_sub,
                owner_sub="op-admin",
                contract_version=BACKPLANE_CONTRACT_VERSION,
                addon_contract_version=BACKPLANE_CONTRACT_VERSION,
                addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
                created_by_sub="op-admin",
            )
        )
    return pairing_id


@pytest.fixture
async def paired_addon(pg_engine: None) -> AsyncIterator[tuple[uuid.UUID, str]]:
    """A single paired add-on: yields ``(pairing_id, service_account_sub)``."""
    sub = f"svc-{uuid.uuid4().hex[:8]}"
    pairing_id = await _seed_pairing(name="automation", service_account_sub=sub)
    yield pairing_id, sub


@_skip_no_docker
async def test_resume_misses_nothing_under_out_of_order_commit(
    paired_addon: tuple[uuid.UUID, str],
) -> None:
    """The per-pairing lock forbids a committed higher ``seq`` ahead of an
    uncommitted lower one, so a resume cursor walk misses nothing.

    Stages the exact interleaving the raw ``seq > after`` cursor was unsafe
    under: writer A draws the lower ``seq`` and holds it uncommitted while
    writer B tries to write the same pairing. Without serialization B would
    draw a higher ``seq`` and commit first, letting a reader skip A. With
    the lock B is blocked until A commits, proven by the empty read while A
    still holds its uncommitted row.
    """
    pairing_id, sub = paired_addon
    svc = AddonStepEventService()
    sm = get_sessionmaker()

    # Writer A: acquire the per-pairing lock + draw the lower seq, but do
    # NOT commit yet. The session stays open holding the xact lock.
    session_a = sm()
    try:
        row_a = await svc.record_if_owned(
            session_a,
            tenant_id=TENANT_A_ID,
            owner_principal_sub=sub,
            event_kind="event.a",
            work_ref=None,
            audit_id=None,
            payload={"which": "a"},
        )
        assert row_a is not None
        seq_a = row_a.seq

        # Writer B: a concurrent committed write to the SAME pairing. It
        # must block at the advisory lock A holds — it cannot draw its seq.
        task_b = asyncio.create_task(
            svc.record_if_owned_committed(
                tenant_id=TENANT_A_ID,
                owner_principal_sub=sub,
                event_kind="event.b",
                work_ref=None,
                audit_id=None,
                payload={"which": "b"},
            )
        )
        # Give B time to reach the lock and block on it.
        await asyncio.sleep(0.5)
        assert not task_b.done(), "writer B was not serialized behind A's lock"

        # The anti-bug assertion: with A's lower seq still uncommitted and B
        # blocked, the durable log holds NO event. Without the lock B would
        # have committed its higher seq here, and a reader advancing its
        # cursor to it would permanently skip A.
        pre = await svc.list_for_pairing(pairing_id=pairing_id, after_seq=0, limit=100)
        assert pre.items == []

        await session_a.commit()  # release the lock; A's seq becomes visible
    finally:
        await session_a.close()

    # B now unblocks, draws a seq strictly above A's, and commits.
    await asyncio.wait_for(task_b, timeout=10)

    # Commit order == seq order per pairing: A (committed first) has the
    # lower seq; B the higher.
    async with sm() as session:
        rows = (
            (
                await session.execute(
                    select(AddonStepEvent)
                    .where(AddonStepEvent.pairing_id == pairing_id)
                    .order_by(AddonStepEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [r.event_kind for r in rows] == ["event.a", "event.b"]
    assert rows[0].seq == seq_a
    assert rows[1].seq > seq_a

    # Full durable-resume walk, one event per page: stepping the cursor
    # forward yields both events in seq order with no gap and no repeat.
    seen: list[str] = []
    cursor = 0
    while True:
        page = await svc.list_for_pairing(pairing_id=pairing_id, after_seq=cursor, limit=1)
        if not page.items:
            break
        seen.append(page.items[0].event_kind)
        assert page.next_cursor is not None
        cursor = int(page.next_cursor)
    assert seen == ["event.a", "event.b"]


@_skip_no_docker
async def test_lock_is_per_pairing_and_does_not_block_a_sibling(
    paired_addon: tuple[uuid.UUID, str],
) -> None:
    """The seq lock is scoped per pairing — a write to a different pairing
    does not block behind it, so serialization is not a global bottleneck.
    """
    pairing_a, sub_a = paired_addon
    sub_b = f"svc-{uuid.uuid4().hex[:8]}"
    pairing_b = await _seed_pairing(name="ssp", service_account_sub=sub_b)
    svc = AddonStepEventService()
    sm = get_sessionmaker()

    session_a = sm()
    try:
        row_a = await svc.record_if_owned(
            session_a,
            tenant_id=TENANT_A_ID,
            owner_principal_sub=sub_a,
            event_kind="pairing.a",
            work_ref=None,
            audit_id=None,
            payload={},
        )
        assert row_a is not None

        # A different pairing's committed write must complete while A still
        # holds pairing_a's lock (different lock key -> no contention).
        await asyncio.wait_for(
            svc.record_if_owned_committed(
                tenant_id=TENANT_A_ID,
                owner_principal_sub=sub_b,
                event_kind="pairing.b",
                work_ref=None,
                audit_id=None,
                payload={},
            ),
            timeout=5,
        )

        async with sm() as session:
            b_count = (
                await session.execute(
                    select(func.count())
                    .select_from(AddonStepEvent)
                    .where(AddonStepEvent.pairing_id == pairing_b)
                )
            ).scalar_one()
        assert b_count == 1

        await session_a.commit()
    finally:
        await session_a.close()

    async with sm() as session:
        a_count = (
            await session.execute(
                select(func.count())
                .select_from(AddonStepEvent)
                .where(AddonStepEvent.pairing_id == pairing_a)
            )
        ).scalar_one()
    assert a_count == 1
