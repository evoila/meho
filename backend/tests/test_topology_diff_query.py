# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for :func:`query_diff` (G9.3-T4 #860).

Coverage matrix (Task #860 acceptance criteria):

* **Window bounds** -- ``ts1`` is exclusive, ``ts2`` is inclusive.
* **Net-fold rule** -- a resource's in-window history rows fold to
  one of ``created`` / ``updated`` / ``removed``:

  - First in-window row ``created``, last not ``removed`` -> ``created``.
  - Last in-window row ``removed`` -> ``removed`` (including the
    created-and-removed-in-same-window case).
  - Otherwise -> ``updated``.

* **Tenant scoping** -- cross-tenant rows never enter the fold.
* **``changed_only`` suppresses ``last_seen``-only updates** -- the
  refresh-service heartbeat shape that the operator wants to filter out
  when scanning a diff for substantive change.
* **1000-row hard cap** -- seeded high-churn fixture asserts
  ``truncated=True`` plus the canonical remediation hint when the
  cohort of changed resources exceeds the cap.
* **``kind_filter`` narrows after the fold** -- the cap fires on the
  post-filter cohort, not the pre-filter set.
* **Inverted window raises** -- ``ts1 >= ts2`` raises
  :class:`ValueError`.
* **Tombstone replay** -- a ``removed`` row whose ``node_id`` is NULL
  (post-window ``ON DELETE SET NULL``) still surfaces with the kind /
  name lifted from the ``before`` snapshot side.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`. Rows are
seeded directly via ``get_sessionmaker()`` -- the diff-on-write hook
(T2 #857) is the real write path but T4's contract is the *read*
substrate; T2 write semantics are exercised by
:mod:`tests.test_topology_history_hook`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

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
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.topology.query import (
    _DIFF_FETCH_RESOURCE_CAP,
    _DIFF_NODE_SQL,
    query_diff,
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
    """Construct an :class:`Operator` for the diff call."""
    return Operator(
        sub="operator-test",
        name="Test Operator",
        email=None,
        raw_jwt="not-a-real-token",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_tenant(session: AsyncSession, *, slug: str = "tenant-a") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    return tenant_id


async def _seed_node(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    kind: str = "vm",
) -> uuid.UUID:
    node_id = uuid.uuid4()
    session.add(
        GraphNode(
            id=node_id,
            tenant_id=tenant_id,
            kind=kind,
            name=name,
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


#: Fixed UUID baked into the snapshot fixture builder so before / after
#: snapshots for the same logical resource compare equal under
#: ``--changed-only`` (the projected ``id`` field doesn't change across
#: a refresh-heartbeat update; only ``last_seen`` does). Callers that
#: need distinct resources pass ``extra={"id": ...}``.
_FIXED_SNAPSHOT_NODE_ID = str(uuid.uuid4())


def _node_snapshot(
    *,
    name: str,
    kind: str = "vm",
    last_seen: str = "2026-05-22T09:00:00",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a node-side snapshot projection.

    Mirrors the column set :data:`history._NODE_SNAPSHOT_COLUMNS` writes
    so ``--changed-only`` correctly identifies ``last_seen``-only diffs.
    The projected ``id`` is fixed (not a fresh random UUID per call) so
    two consecutive snapshot builds for the same logical resource
    compare equal on every column except the one the test mutates --
    the diff-on-write hook's real snapshot pair captures the same id
    on both sides.
    """
    body: dict[str, Any] = {
        "id": _FIXED_SNAPSHOT_NODE_ID,
        "kind": kind,
        "name": name,
        "target_id": None,
        "properties": {},
        "discovered_by": "vmware",
        "first_seen": "2026-05-22T08:00:00",
        "last_seen": last_seen,
    }
    if extra is not None:
        body.update(extra)
    return body


async def _seed_node_history(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID | None,
    valid_from: datetime,
    change_kind: GraphHistoryChangeKind,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    audit_id: uuid.UUID | None = None,
) -> None:
    session.add(
        GraphNodeHistory(
            node_id=node_id,
            tenant_id=tenant_id,
            change_kind=change_kind.value,
            snapshot={"before": before, "after": after},
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
    change_kind: GraphHistoryChangeKind,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    audit_id: uuid.UUID | None = None,
) -> None:
    session.add(
        GraphEdgeHistory(
            edge_id=edge_id,
            tenant_id=tenant_id,
            change_kind=change_kind.value,
            snapshot={"before": before, "after": after},
            audit_id=audit_id or uuid.uuid4(),
            valid_from=valid_from,
        )
    )


# ---------------------------------------------------------------------------
# Window bounds (ts1 exclusive, ts2 inclusive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_ts1_exclusive_ts2_inclusive(session: AsyncSession) -> None:
    """``valid_from > ts1`` and ``valid_from <= ts2``."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # Three rows at base, base+1s, base+2s.
    for i in range(3):
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
            change_kind=GraphHistoryChangeKind.UPDATED,
            before=_node_snapshot(name="vm-a"),
            after=_node_snapshot(name="vm-a", extra={"properties": {"v": i}}),
        )
    await session.commit()

    # ts1 = base (exclusive), ts2 = base + 1s (inclusive) -> only the
    # middle row falls in window.
    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=1),
    )
    assert len(result.entries) == 1
    assert result.entries[0].change_kind == "updated"


# ---------------------------------------------------------------------------
# Net-fold rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fold_created_when_first_in_window(session: AsyncSession) -> None:
    """A resource with ``created`` first in window folds to ``created``."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-new")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after=_node_snapshot(name="vm-new"),
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=2),
        change_kind=GraphHistoryChangeKind.UPDATED,
        before=_node_snapshot(name="vm-new"),
        after=_node_snapshot(name="vm-new", extra={"properties": {"v": 1}}),
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.change_kind == "created"
    assert entry.name == "vm-new"
    assert entry.source == "node"


@pytest.mark.asyncio
async def test_fold_removed_when_last_in_window(session: AsyncSession) -> None:
    """A resource whose last in-window row is ``removed`` folds to ``removed``."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-doomed")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.UPDATED,
        before=_node_snapshot(name="vm-doomed"),
        after=_node_snapshot(name="vm-doomed", extra={"properties": {"v": 1}}),
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=2),
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=_node_snapshot(name="vm-doomed"),
        after=None,
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.change_kind == "removed"
    assert entry.name == "vm-doomed"


@pytest.mark.asyncio
async def test_fold_created_and_removed_in_same_window_nets_removed(
    session: AsyncSession,
) -> None:
    """``created`` then ``removed`` in the same window nets to ``removed``."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-ephemeral")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after=_node_snapshot(name="vm-ephemeral"),
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=2),
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=_node_snapshot(name="vm-ephemeral"),
        after=None,
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 1
    assert result.entries[0].change_kind == "removed"


@pytest.mark.asyncio
async def test_fold_updated_when_first_row_isnt_created(
    session: AsyncSession,
) -> None:
    """In-window mutations on a pre-existing row fold to ``updated``."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-old")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.UPDATED,
        before=_node_snapshot(name="vm-old"),
        after=_node_snapshot(name="vm-old", extra={"properties": {"v": 1}}),
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 1
    assert result.entries[0].change_kind == "updated"


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_scoping_isolates_diff(session: AsyncSession) -> None:
    """Diff for tenant A never includes tenant B's history rows."""
    tenant_a = await _seed_tenant(session, slug="tenant-a")
    tenant_b = await _seed_tenant(session, slug="tenant-b")
    node_a = await _seed_node(session, tenant_a, name="vm-a")
    node_b = await _seed_node(session, tenant_b, name="vm-b")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_a,
        node_id=node_a,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        after=_node_snapshot(name="vm-a"),
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_b,
        node_id=node_b,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        after=_node_snapshot(name="vm-b"),
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_a),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 1
    assert result.entries[0].resource_id == node_a


# ---------------------------------------------------------------------------
# changed_only: suppresses last_seen-only updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_only_suppresses_last_seen_only_update(
    session: AsyncSession,
) -> None:
    """An ``updated`` row whose only diff is ``last_seen`` is suppressed."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-heartbeat")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    before = _node_snapshot(name="vm-heartbeat", last_seen="2026-05-22T09:00:00")
    after = _node_snapshot(name="vm-heartbeat", last_seen="2026-05-22T09:15:00")
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.UPDATED,
        before=before,
        after=after,
    )
    await session.commit()

    # Without --changed-only: the heartbeat surfaces.
    result_all = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result_all.entries) == 1

    # With --changed-only: the heartbeat is suppressed.
    result_filtered = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
        changed_only=True,
    )
    assert result_filtered.entries == ()


@pytest.mark.asyncio
async def test_changed_only_keeps_substantive_update(
    session: AsyncSession,
) -> None:
    """A substantive ``updated`` row survives ``changed_only``."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-real")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    before = _node_snapshot(name="vm-real", extra={"properties": {"v": 0}})
    after = _node_snapshot(name="vm-real", extra={"properties": {"v": 1}})
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.UPDATED,
        before=before,
        after=after,
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
        changed_only=True,
    )
    assert len(result.entries) == 1
    assert result.entries[0].change_kind == "updated"


@pytest.mark.asyncio
async def test_changed_only_keeps_created_and_removed_entries(
    session: AsyncSession,
) -> None:
    """``created`` / ``removed`` are never refresh heartbeats; always surface."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-new")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after=_node_snapshot(name="vm-new"),
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
        changed_only=True,
    )
    assert len(result.entries) == 1
    assert result.entries[0].change_kind == "created"


# ---------------------------------------------------------------------------
# 1000-row hard cap (load-bearing acceptance criterion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_churn_fixture_truncates_at_1000_rows(
    session: AsyncSession,
) -> None:
    """Seed 1100 created resources; 1099 fall strictly after ts1; cap is 1000.

    The 1000-row hard cap with truncation marker + 'narrow the time
    window' hint is the load-bearing AC for Task #860 -- the cap
    protects the front from a hostile / wide time window where every
    resource in a churning tenant landed in the same diff.

    Off-by-one note: ``ts1`` is exclusive (``valid_from > ts1``), and the
    test seeds the first row at ``valid_from == base`` (``i == 0``), so
    that row falls **outside** the queried window. Only 1099 in-window
    rows are eligible -- still well above the 1000 cap, so the
    truncation assertion below remains stable regardless of the
    boundary-inclusive vs boundary-exclusive question.
    """
    tenant_id = await _seed_tenant(session)
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    # 1100 distinct created nodes, each with one in-window history row.
    for i in range(1100):
        node_id = await _seed_node(session, tenant_id, name=f"vm-{i:04d}")
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i),
            change_kind=GraphHistoryChangeKind.CREATED,
            before=None,
            after=_node_snapshot(name=f"vm-{i:04d}"),
        )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=2000),
    )
    assert result.truncated is True
    assert len(result.entries) == 1000
    assert result.truncation_hint is not None
    # The hint must mention the canonical remediation -- narrow the
    # time window -- so the operator sees the recovery path inline.
    assert "narrow the time window" in result.truncation_hint
    assert "1000" in result.truncation_hint


@pytest.mark.asyncio
async def test_under_cap_returns_not_truncated(session: AsyncSession) -> None:
    """A result below the cap reports ``truncated=False`` + hint is None."""
    tenant_id = await _seed_tenant(session)
    node_id = await _seed_node(session, tenant_id, name="vm-a")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=node_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after=_node_snapshot(name="vm-a"),
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert result.truncated is False
    assert result.truncation_hint is None
    assert len(result.entries) == 1


@pytest.mark.asyncio
async def test_diff_sql_bounds_fetch_at_resource_cap(session: AsyncSession) -> None:
    """The diff statement caps the fetch at the SQL layer, not after fetchall (#987).

    Seeds well over the per-side fetch ceiling of distinct resources,
    then executes ``_DIFF_NODE_SQL`` with the production ``resource_cap``
    bind and asserts the statement returns rows for at most
    ``_DIFF_FETCH_RESOURCE_CAP`` distinct resources -- proving a wide
    window over a churning tenant never materialises the whole history
    slice in memory. The +50 slack guarantees the bound is the SQL's
    doing, not the seed count.
    """
    tenant_id = await _seed_tenant(session)
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    seeded = _DIFF_FETCH_RESOURCE_CAP + 50
    for i in range(seeded):
        node_id = await _seed_node(session, tenant_id, name=f"vm-{i:05d}")
        await _seed_node_history(
            session,
            tenant_id=tenant_id,
            node_id=node_id,
            valid_from=base + timedelta(seconds=i + 1),
            change_kind=GraphHistoryChangeKind.CREATED,
            before=None,
            after=_node_snapshot(name=f"vm-{i:05d}"),
        )
    await session.commit()

    result = await session.execute(
        _DIFF_NODE_SQL,
        {
            "tenant_id": tenant_id,
            "ts1": base,
            "ts2": base + timedelta(seconds=seeded + 10),
            "resource_cap": _DIFF_FETCH_RESOURCE_CAP,
        },
    )
    rows = result.fetchall()
    distinct_resources = {row._mapping["resource_id"] for row in rows}
    # Each resource here has exactly one in-window row, so the row count
    # equals the distinct-resource count; both are bounded by the cap.
    assert len(distinct_resources) == _DIFF_FETCH_RESOURCE_CAP
    assert len(rows) == _DIFF_FETCH_RESOURCE_CAP


# ---------------------------------------------------------------------------
# kind_filter narrows after the fold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_filter_narrows_to_one_kind(session: AsyncSession) -> None:
    """``kind_filter`` keeps only entries whose domain ``kind`` matches."""
    tenant_id = await _seed_tenant(session)
    vm_id = await _seed_node(session, tenant_id, name="vm-a", kind="vm")
    host_id = await _seed_node(session, tenant_id, name="host-a", kind="host")
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=vm_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        after=_node_snapshot(name="vm-a", kind="vm"),
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=host_id,
        valid_from=base + timedelta(seconds=2),
        change_kind=GraphHistoryChangeKind.CREATED,
        after=_node_snapshot(name="host-a", kind="host"),
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
        kind_filter="vm",
    )
    assert len(result.entries) == 1
    assert result.entries[0].kind == "vm"
    assert result.entries[0].name == "vm-a"


# ---------------------------------------------------------------------------
# Edge fold + node fold coexistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_and_edge_entries_both_surface(session: AsyncSession) -> None:
    """The fold returns both node and edge entries."""
    tenant_id = await _seed_tenant(session)
    from_id = await _seed_node(session, tenant_id, name="vm-a")
    to_id = await _seed_node(session, tenant_id, name="host-a", kind="host")
    edge_id = await _seed_edge(session, tenant_id, from_id, to_id)
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=from_id,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.CREATED,
        after=_node_snapshot(name="vm-a"),
    )
    await _seed_edge_history(
        session,
        tenant_id=tenant_id,
        edge_id=edge_id,
        valid_from=base + timedelta(seconds=2),
        change_kind=GraphHistoryChangeKind.CREATED,
        before=None,
        after={"kind": "runs-on", "source": "auto"},
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    sources = {e.source for e in result.entries}
    assert sources == {"node", "edge"}


# ---------------------------------------------------------------------------
# Inverted window raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inverted_window_raises(session: AsyncSession) -> None:
    """``ts1 >= ts2`` is rejected before any DB call."""
    tenant_id = await _seed_tenant(session)
    await session.commit()
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        await query_diff(
            _make_operator(tenant_id),
            ts1=base + timedelta(seconds=10),
            ts2=base,
        )
    with pytest.raises(ValueError):
        await query_diff(
            _make_operator(tenant_id),
            ts1=base,
            ts2=base,
        )


# ---------------------------------------------------------------------------
# Tombstone replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_with_null_resource_id_surfaces(
    session: AsyncSession,
) -> None:
    """A ``removed`` row with ``node_id=NULL`` surfaces with kind / name and
    recovers its resource id from the snapshot (``before.id``)."""
    tenant_id = await _seed_tenant(session)
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=None,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=_node_snapshot(name="vm-tombstone"),
        after=None,
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.change_kind == "removed"
    assert entry.name == "vm-tombstone"
    assert entry.kind == "vm"
    # node_id is NULL on the row, but the deleted resource's id survives in
    # the snapshot and is recovered so the entry still identifies it.
    assert entry.resource_id == uuid.UUID(_FIXED_SNAPSHOT_NODE_ID)


@pytest.mark.asyncio
async def test_two_distinct_tombstones_do_not_collapse(
    session: AsyncSession,
) -> None:
    """Two distinct hard-deleted resources (both ``node_id=NULL``) must surface
    as two separate diff entries, not one merged "deleted resources" entry.

    Regression for the bug where every ``resource_id IS NULL`` tombstone was
    grouped under a single ``None`` key, collapsing distinct deletions into
    one entry, dropping identities, and undercounting the truncation cap.
    """
    tenant_id = await _seed_tenant(session)
    base = datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC)
    other_id = str(uuid.uuid4())
    # Two removed rows, both with node_id=NULL, but distinct snapshot ids.
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=None,
        valid_from=base + timedelta(seconds=1),
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=_node_snapshot(name="vm-deleted-one"),
        after=None,
    )
    await _seed_node_history(
        session,
        tenant_id=tenant_id,
        node_id=None,
        valid_from=base + timedelta(seconds=2),
        change_kind=GraphHistoryChangeKind.REMOVED,
        before=_node_snapshot(name="vm-deleted-two", extra={"id": other_id}),
        after=None,
    )
    await session.commit()

    result = await query_diff(
        _make_operator(tenant_id),
        ts1=base,
        ts2=base + timedelta(seconds=10),
    )
    assert len(result.entries) == 2
    by_id = {entry.resource_id: entry for entry in result.entries}
    assert uuid.UUID(_FIXED_SNAPSHOT_NODE_ID) in by_id
    assert uuid.UUID(other_id) in by_id
    assert by_id[uuid.UUID(_FIXED_SNAPSHOT_NODE_ID)].name == "vm-deleted-one"
    assert by_id[uuid.UUID(other_id)].name == "vm-deleted-two"
    assert all(entry.change_kind == "removed" for entry in result.entries)
