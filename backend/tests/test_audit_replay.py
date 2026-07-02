# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the per-session audit replay (G8.2-T3, #1011).

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`, which migrates a
fresh per-test DB to head before each test. Rows are seeded directly via
``get_sessionmaker()`` so the replay query brain is exercised in isolation from
the write paths. The recursive CTE — the first in the codebase — runs
identically on SQLite and PostgreSQL; the PG-backed multi-level /
tenant-isolation acceptance is the T7 integration job, so this module is the
unit-level coverage of the contract.

Coverage matrix (#1011 acceptance criteria):

* Tree nesting — root → child → grandchild + a sibling root replays to the
  correct shape, each branch ``occurred_at``-ascending, roots chronological.
* Tenant isolation — tenant-B rows sharing the same ``agent_session_id`` are
  unreachable from a tenant-A ``replay_session(..., tenant_id=A)`` call.
* Cycle defence — a self-referential row (``parent_audit_id == id``) and a
  2-row mutual cycle both terminate without infinite recursion.
* ``max_depth`` — a deep chain truncates at the cap; the capped node keeps its
  row but no children.
* Flat session — no ``parent_audit_id`` links renders a flat list of roots.
* Empty session — a session id with no rows returns ``[]``; chassis HTTP rows
  (NULL ``agent_session_id``) are unreachable by session id.
* NULL-session lineage — a child whose own ``agent_session_id`` is NULL but
  whose ``parent_audit_id`` points into the session is pulled in by the CTE.
* ``ReplayNode`` shape — carries every ``AuditEntry`` field plus ``depth`` +
  ``children``; the public import works.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import AuditEntry, ReplayNode, replay_session
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings


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


_BASE = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


async def _seed_audit_row(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    second: int,
    agent_session_id: uuid.UUID | None = None,
    parent_audit_id: uuid.UUID | None = None,
    row_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one :class:`AuditLog` row at ``_BASE + second`` and return its id."""
    row_id = row_id or uuid.uuid4()
    s.add(
        AuditLog(
            id=row_id,
            occurred_at=_BASE + timedelta(seconds=second),
            operator_sub="operator-1",
            tenant_id=tenant_id,
            method="POST",
            path="/mcp",
            status_code=200,
            duration_ms=Decimal("1.0"),
            payload={"op_id": "vsphere.vm.list", "op_class": "read"},
            agent_session_id=agent_session_id,
            parent_audit_id=parent_audit_id,
        ),
    )
    return row_id


def _ids(nodes: list[ReplayNode]) -> list[uuid.UUID]:
    """Root-level ids in returned order."""
    return [n.id for n in nodes]


# ---------------------------------------------------------------------------
# Tree nesting + ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tree_nesting_and_chronological_order(session: AsyncSession) -> None:
    """root → child → grandchild + a sibling root replays to the correct shape."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()

    root = await _seed_audit_row(session, tenant_id=tenant_id, second=0, agent_session_id=sess)
    child = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=1,
        agent_session_id=sess,
        parent_audit_id=root,
    )
    grandchild = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=2,
        agent_session_id=sess,
        parent_audit_id=child,
    )
    # Sibling root — later than the first root, so it sorts second.
    sibling_root = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=3,
        agent_session_id=sess,
    )
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session)

    assert _ids(roots) == [root, sibling_root]
    assert roots[0].depth == 0
    assert _ids(roots[0].children) == [child]
    assert roots[0].children[0].depth == 1
    assert _ids(roots[0].children[0].children) == [grandchild]
    assert roots[0].children[0].children[0].depth == 2
    assert roots[1].children == []


@pytest.mark.asyncio
async def test_children_branch_is_occurred_at_ascending(session: AsyncSession) -> None:
    """Children of a node are ordered ascending by ``occurred_at``."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    root = await _seed_audit_row(session, tenant_id=tenant_id, second=0, agent_session_id=sess)
    # Seed the later child first to prove ordering is by occurred_at, not insertion.
    late = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=5,
        agent_session_id=sess,
        parent_audit_id=root,
    )
    early = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=2,
        agent_session_id=sess,
        parent_audit_id=root,
    )
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session)

    assert _ids(roots[0].children) == [early, late]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_same_session_id(session: AsyncSession) -> None:
    """Tenant B's rows under the same session id are unreachable from tenant A."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    sess = uuid.uuid4()  # identical session id on both tenants

    a_root = await _seed_audit_row(session, tenant_id=tenant_a, second=0, agent_session_id=sess)
    await _seed_audit_row(session, tenant_id=tenant_b, second=0, agent_session_id=sess)
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_a, session=session)

    assert _ids(roots) == [a_root]


@pytest.mark.asyncio
async def test_tenant_isolation_cross_tenant_parent_link(session: AsyncSession) -> None:
    """A cross-tenant ``parent_audit_id`` cannot pull a foreign row into the tree.

    Tenant A anchors the session; a tenant-B row claims a tenant-A row as its
    parent. The recursive arm re-asserts ``tenant_id`` so the foreign child is
    never added.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    sess = uuid.uuid4()

    a_root = await _seed_audit_row(session, tenant_id=tenant_a, second=0, agent_session_id=sess)
    # Tenant-B row pointing at tenant-A's root — must not be reachable.
    await _seed_audit_row(
        session,
        tenant_id=tenant_b,
        second=1,
        agent_session_id=None,
        parent_audit_id=a_root,
    )
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_a, session=session)

    assert _ids(roots) == [a_root]
    assert roots[0].children == []


# ---------------------------------------------------------------------------
# Cycle defence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_referential_row_terminates(session: AsyncSession) -> None:
    """A row whose ``parent_audit_id == id`` is treated as a root, not a loop."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    row_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=0,
        agent_session_id=sess,
        parent_audit_id=row_id,
        row_id=row_id,
    )
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session)

    assert _ids(roots) == [row_id]
    assert roots[0].depth == 0
    assert roots[0].children == []


@pytest.mark.asyncio
async def test_two_row_mutual_cycle_terminates(session: AsyncSession) -> None:
    """An A <-> B mutual cycle terminates and surfaces both rows exactly once.

    Both rows reference each other as parent, so neither qualifies as a natural
    (NULL / external / self) root; the whole component is a cycle. The
    depth-bounded CTE keeps the SQL closure fetch from looping forever, then
    tree assembly promotes the chronologically-first orphan (A) to a root,
    walks down to B as its child, and drops the B -> A back-edge via the
    path-set. The contract: termination + every fetched row present once.
    """
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=0,
        agent_session_id=sess,
        parent_audit_id=b_id,
        row_id=a_id,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=1,
        agent_session_id=sess,
        parent_audit_id=a_id,
        row_id=b_id,
    )
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session, max_depth=20)

    seen: list[uuid.UUID] = []

    def _collect(node: ReplayNode) -> None:
        seen.append(node.id)
        for c in node.children:
            _collect(c)

    for r in roots:
        _collect(r)

    # Each row appears exactly once (no infinite expansion), and both are present.
    assert sorted(seen) == sorted([a_id, b_id])
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_max_depth_truncates_deep_chain(session: AsyncSession) -> None:
    """A chain deeper than ``max_depth`` truncates; the capped node has no children."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    parent: uuid.UUID | None = None
    chain: list[uuid.UUID] = []
    for i in range(6):
        node = await _seed_audit_row(
            session,
            tenant_id=tenant_id,
            second=i,
            agent_session_id=sess,
            parent_audit_id=parent,
        )
        chain.append(node)
        parent = node
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session, max_depth=3)

    # Walk to the deepest reachable node and assert depth caps at 3.
    node = roots[0]
    depths = [node.depth]
    while node.children:
        node = node.children[0]
        depths.append(node.depth)
    assert depths == [0, 1, 2, 3]
    assert node.children == []  # depth-3 node is truncated despite deeper rows


# ---------------------------------------------------------------------------
# Flat / empty / NULL-session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flat_session_renders_as_root_list(session: AsyncSession) -> None:
    """No ``parent_audit_id`` links → a flat chronological list of roots."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    ids = [
        await _seed_audit_row(session, tenant_id=tenant_id, second=i, agent_session_id=sess)
        for i in range(4)
    ]
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session)

    assert _ids(roots) == ids
    assert all(r.depth == 0 and r.children == [] for r in roots)


@pytest.mark.asyncio
async def test_empty_session_returns_empty_list(session: AsyncSession) -> None:
    """A session id with no rows replays to ``[]``."""
    tenant_id = uuid.uuid4()
    roots = await replay_session(uuid.uuid4(), tenant_id=tenant_id, session=session)
    assert roots == []


@pytest.mark.asyncio
async def test_chassis_http_rows_unreachable_by_session(session: AsyncSession) -> None:
    """Chassis HTTP rows (NULL ``agent_session_id``) are not anchors for replay."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    # NULL-session chassis row, unrelated to the queried session.
    await _seed_audit_row(session, tenant_id=tenant_id, second=0, agent_session_id=None)
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session)
    assert roots == []


@pytest.mark.asyncio
async def test_null_session_child_captured_via_parent_link(session: AsyncSession) -> None:
    """A NULL-session child is pulled in by the CTE via its ``parent_audit_id``.

    This is the belt-and-suspenders the recursive CTE adds over a flat
    ``WHERE agent_session_id = :id``: a composite ``dispatch_child`` row whose
    session contextvar didn't propagate (NULL ``agent_session_id``) still
    appears in the tree because it is linked to an anchored parent.
    """
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    root = await _seed_audit_row(session, tenant_id=tenant_id, second=0, agent_session_id=sess)
    null_child = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        second=1,
        agent_session_id=None,  # session id did not propagate
        parent_audit_id=root,
    )
    await session.commit()

    roots = await replay_session(sess, tenant_id=tenant_id, session=session)

    assert _ids(roots) == [root]
    assert _ids(roots[0].children) == [null_child]


# ---------------------------------------------------------------------------
# ReplayNode shape
# ---------------------------------------------------------------------------


def test_replay_node_carries_every_audit_entry_field() -> None:
    """``ReplayNode`` is an ``AuditEntry`` subtype plus ``depth`` + ``children``."""
    assert issubclass(ReplayNode, AuditEntry)
    node_fields = set(ReplayNode.model_fields)
    entry_fields = set(AuditEntry.model_fields)
    assert entry_fields <= node_fields
    assert node_fields - entry_fields == {"depth", "children"}


def test_replay_node_is_frozen() -> None:
    """``ReplayNode`` is immutable once constructed."""
    node = ReplayNode(
        id=uuid.uuid4(),
        ts=_BASE,
        tenant_id=uuid.uuid4(),
        principal_sub="op",
        principal_name=None,
        target_id=None,
        target_name=None,
        method="GET",
        path="/mcp",
        status_code=200,
        request_id=None,
        duration_ms=None,
        payload={},
        op_id="vsphere.vm.list",
        op_class="read",
        result_status="ok",
        parent_audit_id=None,
        agent_session_id=None,
        work_ref=None,
        policy_decision=None,
        broadcast_event_id=None,
        depth=0,
        children=[],
    )
    with pytest.raises(ValueError, match="frozen"):
        node.depth = 5  # type: ignore[misc]
