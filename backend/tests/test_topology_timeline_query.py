# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for :func:`query_timeline` (G9.3-T5 #861).

Coverage matrix (Task #861 acceptance criteria):

* **Tenant scoping** -- seed history rows on two tenants; query one;
  cross-tenant rows never returned (first WHERE clause of both
  literal SQL statements is ``tenant_id = :tenant_id``).
* **Cursor forward-pagination is opaque + stable** -- seed 120
  history rows across both tables; walk in ``limit=50`` pages;
  every seeded row returns exactly once; ``next_cursor=None`` on
  the last page. Cursor round-trip via :func:`decode_timeline_cursor`.
* **Stable under concurrent inserts** -- after the first page,
  insert a *new* history row above the cursor's ``valid_from`` (the
  diff-on-write hook landing a new event during the paged sweep);
  the second page does not duplicate or skip any of the original
  rows. The new row appears on a later iteration only if it falls
  below the cursor's keyset position; otherwise it stays outside
  the paged sweep, the expected stability shape.
* **Window scoping** -- ``since`` / ``until`` bound ``valid_from``
  inclusively at both ends.
* **Target scoping** -- nodes filter on ``graph_node.target_id``;
  edges filter on endpoints' ``target_id`` (either endpoint
  qualifies).
* **Cross-table tie-breaker** -- when a node history row and an
  edge history row share ``(valid_from, history_id)``, both appear
  in the result in a deterministic order (the ``"node" > "edge"``
  alphabetical DESC tie-breaker).
* **Tombstone replay** -- a history row whose ``node_id`` /
  ``edge_id`` is NULL (after ``ON DELETE SET NULL`` from a live-row
  delete) still appears in the timeline; the summary falls back
  gracefully.
* **Invalid cursor raises** -- a tampered token raises
  :class:`InvalidTimelineCursorError` from the handler, mirroring
  the audit-query cursor's contract.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`, which
migrates a fresh per-test DB to head before each test (the migration
0012 history tables land via that path). Rows are seeded directly via
``get_sessionmaker()`` -- the diff-on-write hook (T2 #857) is the
real write path but T5's contract is the *read* substrate; the
T2 write path is exercised by :mod:`tests.test_topology_history`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    GraphEdge,
    GraphEdgeHistory,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
    Target,
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.topology.query import query_timeline
from meho_backplane.topology.timeline_cursor import (
    InvalidTimelineCursorError,
    decode_timeline_cursor,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Keycloak + Vault env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test, scoped to a single ``async with``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(tenant_id: uuid.UUID) -> Operator:
    """Construct an :class:`Operator` for the timeline call.

    The handler reads only ``operator.tenant_id``; the other fields
    are populated to satisfy the frozen Pydantic model. ``raw_jwt``
    is a placeholder -- the substrate does not crack the token.
    """
    return Operator(
        sub="operator-test",
        name="Test Operator",
        email=None,
        raw_jwt="not-a-real-token",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(session: AsyncSession, *, slug: str = "tenant-a") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its UUID."""
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    return tenant_id


async def _seed_target(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str = "vc-prod",
    product: str = "vsphere",
) -> uuid.UUID:
    """Insert a :class:`Target` row and return its UUID."""
    target_id = uuid.uuid4()
    session.add(
        Target(
            id=target_id,
            tenant_id=tenant_id,
            name=name,
            aliases=[],
            product=product,
            host=f"{name}.example.com",
            auth_model="shared_service_account",
            extras={},
        )
    )
    return target_id


async def _seed_node(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    kind: str = "vm",
    target_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a :class:`GraphNode` row and return its UUID."""
    node_id = uuid.uuid4()
    session.add(
        GraphNode(
            id=node_id,
            tenant_id=tenant_id,
            kind=kind,
            name=name,
            target_id=target_id,
            discovered_by="vmware",
        )
    )
    return node_id


async def _seed_edge(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    *,
    kind: str = "runs-on",
) -> uuid.UUID:
    """Insert a :class:`GraphEdge` row and return its UUID."""
    edge_id = uuid.uuid4()
    session.add(
        GraphEdge(
            id=edge_id,
            tenant_id=tenant_id,
            from_node_id=from_id,
            to_node_id=to_id,
            kind=kind,
            source="auto",
            discovered_by="vmware",
        )
    )
    return edge_id


async def _seed_node_history(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID | None,
    valid_from: datetime,
    change_kind: GraphHistoryChangeKind = GraphHistoryChangeKind.CREATED,
    snapshot: dict[str, object] | None = None,
    audit_id: uuid.UUID | None = None,
) -> None:
    """Insert one :class:`GraphNodeHistory` row.

    Default snapshot carries an ``after`` projection with ``kind`` +
    ``name`` so the summary renderer has something to render.
    """
    if snapshot is None:
        snapshot = {"before": None, "after": {"kind": "vm", "name": "vm-test"}}
    session.add(
        GraphNodeHistory(
            node_id=node_id,
            tenant_id=tenant_id,
            change_kind=change_kind.value,
            snapshot=snapshot,
            audit_id=audit_id or uuid.uuid4(),
            valid_from=valid_from,
        )
    )


async def _seed_edge_history(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID | None,
    valid_from: datetime,
    change_kind: GraphHistoryChangeKind = GraphHistoryChangeKind.CREATED,
    snapshot: dict[str, object] | None = None,
    audit_id: uuid.UUID | None = None,
) -> None:
    """Insert one :class:`GraphEdgeHistory` row."""
    if snapshot is None:
        snapshot = {"before": None, "after": {"kind": "runs-on", "source": "auto"}}
    session.add(
        GraphEdgeHistory(
            edge_id=edge_id,
            tenant_id=tenant_id,
            change_kind=change_kind.value,
            snapshot=snapshot,
            audit_id=audit_id or uuid.uuid4(),
            valid_from=valid_from,
        )
    )


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_scoping_isolates_history_rows(session: AsyncSession) -> None:
    """Seed two tenants; query one; cross-tenant history never returned."""
    tenant_a = await _seed_tenant(session, slug="tenant-a")
    tenant_b = await _seed_tenant(session, slug="tenant-b")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    node_a = await _seed_node(session, tenant_a, name="vm-a")
    node_b = await _seed_node(session, tenant_b, name="vm-b")
    await session.flush()
    for i in range(5):
        await _seed_node_history(
            session,
            tenant_id=tenant_a,
            node_id=node_a,
            valid_from=base + timedelta(seconds=i),
        )
        await _seed_node_history(
            session,
            tenant_id=tenant_b,
            node_id=node_b,
            valid_from=base + timedelta(seconds=i),
        )
    await session.commit()

    result = await query_timeline(_make_operator(tenant_a))

    assert len(result.rows) == 5
    # Every row belongs to tenant_a — verified indirectly by its
    # resource_id: those are tenant_a's node ids.
    assert all(row.resource_id == node_a for row in result.rows)


@pytest.mark.asyncio
async def test_tenant_scoping_no_rows_for_empty_tenant(session: AsyncSession) -> None:
    """An empty tenant produces an empty timeline (zero rows, no error)."""
    tenant_a = await _seed_tenant(session)
    await session.commit()
    result = await query_timeline(_make_operator(tenant_a))
    assert result.rows == ()
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# Cursor pagination (AC: forward cursor is opaque + stable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_pagination_walks_120_rows_in_3_pages(
    session: AsyncSession,
) -> None:
    """120 rows split across both tables / ``limit=50`` → 3 pages.

    Final page carries ``next_cursor=None``. Every seeded row
    appears exactly once across the sweep -- the stability invariant.
    """
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    edge_to = await _seed_node(session, tenant_id, name="host-a", kind="host")
    edge_id = await _seed_edge(session, tenant_id, node_id, edge_to)
    await session.flush()

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # 60 node-history rows + 60 edge-history rows = 120 total. Two
    # rows per timestamp so the global ordering exercises the
    # tie-breaker.
    for i in range(60):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
        )
        await _seed_edge_history(
            session,
            tenant_id=tenant_id,
            edge_id=edge_id,
            valid_from=base + timedelta(seconds=i),
        )
    await session.commit()

    seen_history_keys: set[tuple[str, int]] = set()
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        result = await query_timeline(
            _make_operator(tenant_id),
            limit=50,
            cursor=cursor,
        )
        for row in result.rows:
            key = (row.source, row.history_id)
            assert key not in seen_history_keys, f"duplicate row across pages: {key}"
            seen_history_keys.add(key)
        if result.next_cursor is None:
            break
        cursor = result.next_cursor

    assert pages == 3
    assert len(seen_history_keys) == 120


@pytest.mark.asyncio
async def test_cursor_terminal_page_when_under_limit(session: AsyncSession) -> None:
    """Fewer rows than ``limit`` → ``next_cursor`` is None immediately."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(5):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
        )
    await session.commit()

    result = await query_timeline(_make_operator(tenant_id), limit=50)
    assert len(result.rows) == 5
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_cursor_next_cursor_decodes_to_last_row(session: AsyncSession) -> None:
    """``next_cursor`` round-trips to the page's last ``(valid_from,
    history_id, source)`` position."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(10):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
        )
    await session.commit()

    result = await query_timeline(_make_operator(tenant_id), limit=3)

    assert result.next_cursor is not None
    last = result.rows[-1]
    pos = decode_timeline_cursor(result.next_cursor)
    assert pos.ts.replace(tzinfo=None) == last.valid_from.replace(tzinfo=None)
    assert pos.history_id == last.history_id
    assert pos.source == last.source


@pytest.mark.asyncio
async def test_cursor_invalid_token_raises(session: AsyncSession) -> None:
    """A tampered cursor raises :class:`InvalidTimelineCursorError`."""
    tenant_id = await _seed_tenant(session)
    await session.commit()
    with pytest.raises(InvalidTimelineCursorError):
        await query_timeline(_make_operator(tenant_id), cursor="not%%base64$$")


@pytest.mark.asyncio
async def test_cursor_stable_under_concurrent_insert(session: AsyncSession) -> None:
    """A new history row landing between page 1 and page 2 doesn't break paging.

    The diff-on-write hook (T2 #857) writes history rows continuously
    as the live graph mutates. The cursor's keyset compare
    ``(valid_from, history_id) < cursor`` means a row landing
    *above* the cursor's keyset position is *not* visited by the
    paged sweep -- the operator sees the timeline as it existed at
    page 1's snapshot, not a mid-sweep moving target. A row landing
    *below* the cursor would naturally appear on a later page. Either
    way: no row is duplicated, no original row is skipped.
    """
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    seeded_ids: list[int] = []
    for i in range(75):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
        )
    await session.commit()

    # Page 1.
    page1 = await query_timeline(_make_operator(tenant_id), limit=50)
    seeded_ids.extend(r.history_id for r in page1.rows)
    assert page1.next_cursor is not None

    # Simulate the diff-on-write hook landing a fresh history row
    # ABOVE the page-1 last row's ``valid_from`` (i.e. newer than
    # anything we've paged through). This row should not be visited
    # by the next page -- the cursor's keyset compare excludes it.
    above_ts = base + timedelta(seconds=200)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=above_ts,
    )
    await session.commit()

    # Page 2 with the cursor from page 1.
    page2 = await query_timeline(
        _make_operator(tenant_id),
        limit=50,
        cursor=page1.next_cursor,
    )
    page2_ids = [r.history_id for r in page2.rows]

    # No duplicate id across pages.
    assert set(seeded_ids).isdisjoint(set(page2_ids))
    # The new "above the cursor" row was not visited -- the paged
    # sweep is stable against concurrent inserts above the cursor.
    assert all(r.valid_from < page1.rows[-1].valid_from for r in page2.rows)

    # Sweep completes; the remaining 25 originals all appeared.
    seeded_ids.extend(page2_ids)
    assert len(seeded_ids) == 75


# ---------------------------------------------------------------------------
# Window scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_until_bounds_inclusive(session: AsyncSession) -> None:
    """``since`` / ``until`` bound ``valid_from`` inclusively at both ends."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(10):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(minutes=i),
        )
    await session.commit()

    # Window covers minutes 3..7 inclusive on both ends → 5 rows.
    result = await query_timeline(
        _make_operator(tenant_id),
        since=base + timedelta(minutes=3),
        until=base + timedelta(minutes=7),
    )
    assert len(result.rows) == 5
    # SQLite strips tzinfo on read-back; compare the naive form on
    # both sides so the assertion is dialect-portable.
    timestamps = [r.valid_from.replace(tzinfo=None) for r in result.rows]
    assert max(timestamps) == (base + timedelta(minutes=7)).replace(tzinfo=None)
    assert min(timestamps) == (base + timedelta(minutes=3)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Target scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_filter_scopes_to_one_targets_resources(
    session: AsyncSession,
) -> None:
    """``target_id`` narrows the timeline to one target's resources."""
    tenant_id = await _seed_tenant(session)
    target_a = await _seed_target(session, tenant_id, name="vc-a")
    target_b = await _seed_target(session, tenant_id, name="vc-b")
    node_a = await _seed_node(session, tenant_id, name="vm-a", target_id=target_a)
    node_b = await _seed_node(session, tenant_id, name="vm-b", target_id=target_b)
    await session.flush()

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_a,
        valid_from=base,
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_b,
        valid_from=base + timedelta(seconds=1),
    )
    await session.commit()

    result = await query_timeline(_make_operator(tenant_id), target_id=target_a)
    assert len(result.rows) == 1
    assert result.rows[0].resource_id == node_a


@pytest.mark.asyncio
async def test_target_filter_includes_edge_when_either_endpoint_matches(
    session: AsyncSession,
) -> None:
    """An edge spanning two targets surfaces in either target's timeline.

    The ``--target`` filter on the edge branch keys on **either**
    endpoint's ``target_id`` (an edge crossing two targets is part of
    both timelines). The node-only branch is covered above; this test
    locks down the edge-history branch of the same filter so a
    regression in the OR-of-endpoint-targets predicate (e.g. an
    accidental AND or a one-sided filter) does not silently drop
    cross-target edges from one side's view.
    """
    tenant_id = await _seed_tenant(session)
    target_a = await _seed_target(session, tenant_id, name="vc-a")
    target_b = await _seed_target(session, tenant_id, name="vc-b")
    node_a = await _seed_node(session, tenant_id, name="vm-a", target_id=target_a)
    node_b = await _seed_node(session, tenant_id, name="vm-b", target_id=target_b)
    edge_id = await _seed_edge(session, tenant_id, node_a, node_b)
    await session.flush()

    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_edge_history(
        session,
        tenant_id=tenant_id,
        edge_id=edge_id,
        valid_from=base,
    )
    await session.commit()

    result_a = await query_timeline(_make_operator(tenant_id), target_id=target_a)
    edge_rows_a = [r for r in result_a.rows if r.source == "edge"]
    assert len(edge_rows_a) == 1, "edge should surface in target_a's timeline"
    assert edge_rows_a[0].resource_id == edge_id

    result_b = await query_timeline(_make_operator(tenant_id), target_id=target_b)
    edge_rows_b = [r for r in result_b.rows if r.source == "edge"]
    assert len(edge_rows_b) == 1, "same edge should surface in target_b's timeline"
    assert edge_rows_b[0].resource_id == edge_id


# ---------------------------------------------------------------------------
# Tombstone replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_returns_tombstone_with_null_resource_id(
    session: AsyncSession,
) -> None:
    """A history row whose ``node_id`` was cleared by ON DELETE SET NULL
    still appears in the timeline.

    The snapshot carries enough state for the summary even when the
    live row is gone.
    """
    tenant_id = await _seed_tenant(session)
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # node_id=None simulates the post-delete tombstone shape.
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=None,
        valid_from=base,
        change_kind=GraphHistoryChangeKind.REMOVED,
        snapshot={"before": {"kind": "vm", "name": "vm-deleted"}, "after": None},
    )
    await session.commit()

    result = await query_timeline(_make_operator(tenant_id))
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.resource_id is None
    assert row.change_kind == "removed"
    assert "vm-deleted" in row.summary
    assert row.source == "node"


# ---------------------------------------------------------------------------
# Ordering and tie-breaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_ordered_newest_first(session: AsyncSession) -> None:
    """Rows return in ``(valid_from DESC, history_id DESC, source DESC)`` order."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    for i in range(5):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
        )
    await session.commit()

    result = await query_timeline(_make_operator(tenant_id))
    # Full keyset ordering check (not just ``valid_from``): the merge
    # in ``query_timeline`` uses ``(valid_from, history_id, source)`` as
    # the descending tie-breaker chain, and cursor stability depends on
    # every level of the chain being respected. Asserting only the
    # outermost ``valid_from`` would miss a regression in the
    # history_id / source tie-breaker that would surface as duplicated
    # or skipped rows across paginated cursors on same-timestamp ties.
    keys = [(r.valid_from, r.history_id, r.source) for r in result.rows]
    assert keys == sorted(keys, reverse=True)


# ---------------------------------------------------------------------------
# Limit validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_limit_raises(session: AsyncSession) -> None:
    """Out-of-range ``limit`` raises :class:`ValueError`."""
    tenant_id = await _seed_tenant(session)
    await session.commit()
    with pytest.raises(ValueError):
        await query_timeline(_make_operator(tenant_id), limit=0)
    with pytest.raises(ValueError):
        await query_timeline(_make_operator(tenant_id), limit=10000)
