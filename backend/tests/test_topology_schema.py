# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`GraphNode` and :class:`GraphEdge`.

Coverage matrix (Task #448 acceptance criteria):

* **Round-trip** -- insert a :class:`GraphNode` / :class:`GraphEdge`,
  query it back, every field survives. Drives the ORM ``default=``
  machinery (uuid, first_seen, properties) against the SQLite dev/test
  driver where the migration's PG server-side defaults are no-ops.
* **Unique constraints** -- ``(tenant_id, kind, name)`` on
  :class:`GraphNode` and ``(tenant_id, from_node_id, to_node_id, kind)``
  on :class:`GraphEdge` both reject duplicate rows with
  :class:`IntegrityError`. Cross-tenant collision is allowed.
* **CHECK constraints** -- ``graph_node.kind`` / ``graph_edge.kind``
  reject a shape-violating value (uppercase, too short, too long) and
  accept any well-formed slug (the vocabulary is open per T1 #2534;
  migration ``0063`` replaced the closed IN-lists with the minimal
  shape CHECK); ``graph_edge.source`` remains a closed enum and
  rejects unknown values with :class:`IntegrityError`.
* **Foreign keys** -- ``graph_node.target_id`` rejects an unknown
  target id, and ``graph_edge.from_node_id`` / ``to_node_id`` reject
  unknown node ids, with :class:`IntegrityError`. SQLite enforces FKs
  only under ``PRAGMA foreign_keys = ON`` (sqlite.org/foreignkeys.html
  §2); each FK test enables the pragma per-session (the shared engine
  uses :class:`StaticPool` so a single connection backs every checkout
  in this test process).
* **Cascade behaviour** -- deleting a :class:`GraphNode` cascade-deletes
  its incident edges; deleting a :class:`~meho_backplane.db.models.Target`
  sets dependent ``graph_node.target_id`` rows to NULL.
* **Schema-level smoke** -- ``alembic upgrade head`` against a fresh
  SQLite DB creates the ``graph_node`` and ``graph_edge`` tables with
  their named indexes; ``upgrade head`` -> ``downgrade 0006`` -> ``upgrade
  head`` is a clean cycle.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` already pre-migrates to ``alembic upgrade head``.
Per-test isolation comes from pytest's ``tmp_path``-scoped DB file, the
same shape every other DB-touching test in the suite uses.

SQLite datetime caveats -- identical to :mod:`tests.test_db_models` /
:mod:`tests.test_db_targets`: SQLite stores datetimes as ISO-8601
strings without timezone information; SQLAlchemy round-trips them as
naive ``datetime`` even when the column is ``DateTime(timezone=True)``.
All datetime assertions strip tzinfo before comparing the wall-clock
parts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    GraphEdge,
    GraphEdgeKind,
    GraphNode,
    Target,
    Tenant,
)
from meho_backplane.settings import get_settings


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection.

    SQLite ships with foreign-key enforcement disabled by default
    (sqlite.org/foreignkeys.html §2). Without this PRAGMA, the
    ``ON DELETE CASCADE`` / ``ON DELETE SET NULL`` cascades declared on
    :attr:`GraphEdge.from_node_id` / :attr:`GraphNode.target_id` and
    the FK-violation :class:`IntegrityError` we expect would both
    silently no-op. The PRAGMA is a per-connection setting; emitting it
    once on the session's bound connection covers every statement that
    follows on the same connection. The shared engine uses
    :class:`StaticPool` on SQLite so a single connection backs every
    checkout in this test process.

    Mirrors the same helper in :mod:`tests.test_db_endpoint_descriptor`;
    kept local rather than hoisted to conftest because the scope
    decision (engine-level vs. per-test) is broader than T1's remit.
    """
    await session.execute(text("PRAGMA foreign_keys = ON"))


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors the pattern in :mod:`tests.test_db_targets`: the autouse
    ``_default_database_url`` fixture only pins ``DATABASE_URL``;
    Keycloak/Vault knobs come from each test file. The
    ``get_settings.cache_clear()`` brackets prevent a stale
    ``Settings`` instance from a previous test from leaking.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, *, slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call.

    Every :class:`GraphNode` / :class:`GraphEdge` carries a real FK to
    :class:`Tenant`, so the per-test setup needs a parent tenant to
    avoid spurious ``IntegrityError`` (under PRAGMA foreign_keys=ON)
    or NULL-violation traps from a stale FK target. The slug is
    parameterised so the cross-tenant tests can seed two distinct
    parents.

    The look-up-then-insert shape is load-bearing: migration ``0018``
    seeds the ``rdc-internal`` tenant into the per-worker schema
    template (:func:`tests.conftest._schema_template_db`), so a plain
    ``session.add(Tenant(slug='rdc-internal', ...))`` would trip
    ``UNIQUE constraint failed: tenant.slug``.
    """
    existing: uuid.UUID | None = await session.scalar(
        select(Tenant.id).where(Tenant.slug == slug),
    )
    if existing is not None:
        return existing
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


# ---------------------------------------------------------------------------
# GraphNode round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_node_round_trip_persists_every_field() -> None:
    """Insert a :class:`GraphNode`, query it back, every field matches."""
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    target_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        # Seed the target so target_id FK is valid.
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="vcenter-prod",
                product="vmware",
                host="vcenter.prod.example.com",
            )
        )
        await session.commit()

        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind="target",
                name="vcenter-prod",
                target_id=target_id,
                properties={"build": "9.0.1.00100", "edition": "Standard"},
                discovered_by="vmware",
                first_seen=now,
                last_seen=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphNode).where(GraphNode.id == node_id))
        row = result.scalar_one()

    assert row.id == node_id
    assert row.tenant_id == tenant_id
    assert row.kind == "target"
    assert row.name == "vcenter-prod"
    assert row.target_id == target_id
    assert row.properties == {"build": "9.0.1.00100", "edition": "Standard"}
    assert row.discovered_by == "vmware"
    assert row.first_seen.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.last_seen is not None
    assert row.last_seen.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_graph_node_orm_defaults_fire_on_sqlite() -> None:
    """``id``, ``first_seen``, ``properties`` populated by ORM defaults.

    The migration's PG-side server defaults (``gen_random_uuid()``,
    ``now()``, ``'{}'::jsonb``) are no-ops on SQLite. The ORM defaults
    must fill them Python-side. A regression that drops any ORM default
    in favour of relying on the migration would surface here as a
    NOT NULL violation on SQLite.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        node = GraphNode(
            tenant_id=tenant_id,
            kind="vm",
            name="vm-test-001",
            discovered_by="vmware",
        )
        session.add(node)
        await session.commit()
        seen_id = node.id
        seen_first = node.first_seen
        seen_props = node.properties
        seen_last = node.last_seen
        seen_target_id = node.target_id

    assert isinstance(seen_id, uuid.UUID)
    assert isinstance(seen_first, datetime)
    assert seen_props == {}
    # last_seen has no default -- refresh sets it on observation.
    assert seen_last is None
    # target_id default is None for inner-graph nodes.
    assert seen_target_id is None


@pytest.mark.asyncio
async def test_graph_node_inner_node_has_null_target_id() -> None:
    """A node that is not itself a target round-trips with ``target_id=None``."""
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind="pod",
                name="ns-default/pod-abc",
                target_id=None,
                discovered_by="kubernetes",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphNode).where(GraphNode.id == node_id))
        row = result.scalar_one()

    assert row.target_id is None
    assert row.kind == "pod"


# ---------------------------------------------------------------------------
# GraphNode unique + check constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_node_tenant_kind_name_uniqueness_enforced() -> None:
    """Two :class:`GraphNode` rows with the same ``(tenant_id, kind, name)`` collide."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            GraphNode(
                tenant_id=tenant_id,
                kind="vm",
                name="dup-vm",
                discovered_by="vmware",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            GraphNode(
                tenant_id=tenant_id,
                kind="vm",
                name="dup-vm",
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_node_same_name_allowed_across_tenants() -> None:
    """Same ``(kind, name)`` in two tenants does not collide.

    The uniqueness constraint includes ``tenant_id`` so cross-tenant
    use of the same handle (e.g. every tenant has a ``vcenter-prod``)
    must be supported. Without ``tenant_id`` in the key, the second
    tenant's insert would fail spuriously.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_a = await _seed_tenant(session, slug="tenant-a")
        tenant_b = await _seed_tenant(session, slug="tenant-b")
        session.add(
            GraphNode(
                tenant_id=tenant_a,
                kind="vm",
                name="shared-name",
                discovered_by="vmware",
            )
        )
        session.add(
            GraphNode(
                tenant_id=tenant_b,
                kind="vm",
                name="shared-name",
                discovered_by="vmware",
            )
        )
        # Must not raise -- different tenants, same (kind, name) is allowed.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphNode).where(GraphNode.name == "shared-name"))
        rows = result.scalars().all()

    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {tenant_a, tenant_b}


@pytest.mark.asyncio
async def test_graph_node_kind_check_accepts_novel_slug() -> None:
    """A well-formed novel ``graph_node.kind`` inserts cleanly (open vocabulary).

    T1 #2534 / migration ``0063``: the closed 14-kind IN-list is gone;
    any lowercase slug passes the DB-layer shape CHECK. Full slug
    validation lives Python-side at the write boundaries.
    """
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind="dns-record",  # not in the old closed enum
                name="www.example.com",
                discovered_by="curated",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        row = (await session.execute(select(GraphNode).where(GraphNode.id == node_id))).scalar_one()
    assert row.kind == "dns-record"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["Bad-Kind", "x", "a" * 64])
async def test_graph_node_kind_check_constraint_rejects_malformed(bad_kind: str) -> None:
    """A shape-violating ``graph_node.kind`` raises :class:`IntegrityError`.

    Migration ``0063``'s minimal shape CHECK (length 2--63, lowercase)
    is the DB-layer backstop for out-of-band inserts; uppercase,
    single-char, and over-long kinds must not land.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            GraphNode(
                tenant_id=tenant_id,
                kind=bad_kind,
                name="bad-kind",
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# GraphEdge round-trip + defaults
# ---------------------------------------------------------------------------


async def _seed_two_nodes(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert two :class:`GraphNode` rows and return their ids.

    Edges need parent nodes that already exist (under PRAGMA
    foreign_keys=ON the FK probe runs at flush time). Helper keeps the
    test bodies focused on the edge invariant under test.
    """
    from_id = uuid.uuid4()
    to_id = uuid.uuid4()
    session.add(
        GraphNode(
            id=from_id,
            tenant_id=tenant_id,
            kind="vm",
            name=f"vm-{from_id.hex[:6]}",
            discovered_by="vmware",
        )
    )
    session.add(
        GraphNode(
            id=to_id,
            tenant_id=tenant_id,
            kind="host",
            name=f"host-{to_id.hex[:6]}",
            discovered_by="vmware",
        )
    )
    await session.commit()
    return from_id, to_id


@pytest.mark.asyncio
async def test_graph_edge_round_trip_persists_every_field() -> None:
    """Insert a :class:`GraphEdge`, query it back, every field matches."""
    sessionmaker = get_sessionmaker()
    edge_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)

        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="runs-on",
                source="auto",
                properties={"observed": "via-vsphere-api"},
                discovered_by="vmware",
                first_seen=now,
                last_seen=now,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphEdge).where(GraphEdge.id == edge_id))
        row = result.scalar_one()

    assert row.id == edge_id
    assert row.tenant_id == tenant_id
    assert row.from_node_id == from_id
    assert row.to_node_id == to_id
    assert row.kind == "runs-on"
    assert row.source == "auto"
    assert row.properties == {"observed": "via-vsphere-api"}
    assert row.discovered_by == "vmware"
    assert row.first_seen.replace(tzinfo=None) == now.replace(tzinfo=None)
    assert row.last_seen is not None
    assert row.last_seen.replace(tzinfo=None) == now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_graph_edge_orm_defaults_fire_on_sqlite() -> None:
    """``id``, ``first_seen``, ``properties`` populated by ORM defaults."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        edge = GraphEdge(
            tenant_id=tenant_id,
            from_node_id=from_id,
            to_node_id=to_id,
            kind="belongs-to",
            source="auto",
            discovered_by="kubernetes",
        )
        session.add(edge)
        await session.commit()
        seen_id = edge.id
        seen_first = edge.first_seen
        seen_props = edge.properties
        seen_last = edge.last_seen

    assert isinstance(seen_id, uuid.UUID)
    assert isinstance(seen_first, datetime)
    assert seen_props == {}
    assert seen_last is None


# ---------------------------------------------------------------------------
# GraphEdge unique + check constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_edge_endpoints_kind_uniqueness_enforced() -> None:
    """Two edges with the same ``(tenant_id, from, to, kind)`` collide.

    The unique index enforces "at most one edge of a given kind
    between a pair within a tenant". A v0.3 multi-edge model would
    replace this with a partial unique; v0.2 keeps it strict.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="runs-on",
                source="auto",
                discovered_by="vmware",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="runs-on",
                source="auto",
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_edge_different_kind_between_same_endpoints_allowed() -> None:
    """Two edges between the same pair of nodes with different ``kind`` coexist.

    The kind axis is part of the unique key, so a VM may both
    ``runs-on`` a host and ``belongs-to`` the same host (hypothetical;
    the example exercises the contract, not the v0.2 vocabulary).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="runs-on",
                source="auto",
                discovered_by="vmware",
            )
        )
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="belongs-to",
                source="auto",
                discovered_by="vmware",
            )
        )
        # Must not raise -- different kinds, same endpoints is allowed.
        await session.commit()


@pytest.mark.asyncio
async def test_graph_edge_kind_check_accepts_novel_slug() -> None:
    """A well-formed novel ``graph_edge.kind`` inserts cleanly (open vocabulary).

    T1 #2534 / migration ``0063``: the closed ten-kind IN-list is gone;
    any lowercase slug passes the DB-layer shape CHECK (`resolves-to`,
    `same-as`, ...). Full slug validation lives Python-side at the
    write boundaries.
    """
    sessionmaker = get_sessionmaker()
    edge_id = uuid.uuid4()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="resolves-to",  # not in the old closed enum
                source="curated",
                discovered_by="curated",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        row = (await session.execute(select(GraphEdge).where(GraphEdge.id == edge_id))).scalar_one()
    assert row.kind == "resolves-to"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["Bad-Kind", "x", "a" * 64])
async def test_graph_edge_kind_check_constraint_rejects_malformed(bad_kind: str) -> None:
    """A shape-violating ``graph_edge.kind`` raises :class:`IntegrityError`.

    Migration ``0063``'s minimal shape CHECK (length 2--63, lowercase)
    is the DB-layer backstop for out-of-band inserts.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind=bad_kind,
                source="auto",
                discovered_by="curated",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_edge_source_check_constraint_rejects_unknown() -> None:
    """A ``graph_edge.source`` outside the closed enum raises :class:`IntegrityError`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="runs-on",
                source="inferred",  # not a member of _GRAPH_EDGE_SOURCES
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Foreign key enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_node_target_id_fk_rejects_bogus_uuid() -> None:
    """``graph_node.target_id`` pointing at an unknown target raises FK error."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        session.add(
            GraphNode(
                tenant_id=tenant_id,
                kind="target",
                name="bogus-target",
                target_id=uuid.uuid4(),  # no Target row matches this id
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_edge_from_node_id_fk_rejects_bogus_uuid() -> None:
    """``graph_edge.from_node_id`` pointing at an unknown node raises FK error."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        _, to_id = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=uuid.uuid4(),  # no GraphNode row matches this id
                to_node_id=to_id,
                kind="runs-on",
                source="auto",
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_graph_edge_to_node_id_fk_rejects_bogus_uuid() -> None:
    """``graph_edge.to_node_id`` pointing at an unknown node raises FK error."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        from_id, _ = await _seed_two_nodes(session, tenant_id)
        session.add(
            GraphEdge(
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=uuid.uuid4(),  # no GraphNode row matches this id
                kind="runs-on",
                source="auto",
                discovered_by="vmware",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------------------
# Cascade behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_edge_cascade_deletes_when_node_deleted() -> None:
    """Hard-deleting a :class:`GraphNode` cascade-deletes its incident edges.

    Refresh's normal path is soft-delete (``last_seen=NULL``); cascade
    only fires under tenant purges + test cleanup. The contract:
    hard-delete of a node does not leave dangling edges that point at a
    missing parent.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        edge_id = uuid.uuid4()
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind="runs-on",
                source="auto",
                discovered_by="vmware",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        node = (
            await session.execute(select(GraphNode).where(GraphNode.id == from_id))
        ).scalar_one()
        await session.delete(node)
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphEdge).where(GraphEdge.id == edge_id))
        remaining = result.scalar_one_or_none()

    assert remaining is None, "ON DELETE CASCADE must remove edges that pointed at the deleted node"


@pytest.mark.asyncio
async def test_graph_node_target_id_set_null_on_target_delete() -> None:
    """Deleting a :class:`Target` sets dependent ``graph_node.target_id`` to NULL.

    Removing an operator-registered target should leave the topology
    data alive in a "no longer a target" form -- agents may still need
    to traverse what *was* an entry point. ``ON DELETE SET NULL``
    enforces that; CASCADE would silently delete the node and every
    edge that touched it.
    """
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    target_id = uuid.uuid4()

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        tenant_id = await _seed_tenant(session)
        session.add(
            Target(
                id=target_id,
                tenant_id=tenant_id,
                name="going-away",
                product="vmware",
                host="vcenter.going-away.example.com",
            )
        )
        await session.commit()
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=tenant_id,
                kind="target",
                name="going-away",
                target_id=target_id,
                discovered_by="vmware",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        await _enable_sqlite_foreign_keys(session)
        target = (await session.execute(select(Target).where(Target.id == target_id))).scalar_one()
        await session.delete(target)
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphNode).where(GraphNode.id == node_id))
        row = result.scalar_one()

    assert row.target_id is None, (
        "ON DELETE SET NULL must clear graph_node.target_id when the target is removed; "
        "left non-null would leave a dangling FK pointing at a deleted row"
    )


# ---------------------------------------------------------------------------
# Schema-level inspection -- migration installs documented tables + indexes
# ---------------------------------------------------------------------------


def _alembic_upgrade_against_fresh_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_filename: str,
) -> tuple[str, Config]:
    """Pin env, reset caches, run ``alembic upgrade head`` on fresh SQLite.

    Shared setup for the sync migration tests below; mirrors the
    helper in :mod:`tests.test_db_targets` and
    :mod:`tests.test_db_endpoint_descriptor`. Returns
    ``(sync_url, alembic_cfg)`` so callers can inspect the resulting
    schema or run further migration ops (upgrade / downgrade).
    """
    from alembic import command

    from meho_backplane.db.migrations import alembic_config

    db_path = tmp_path / db_filename
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    command.upgrade(cfg, "head")
    return sync_url, cfg


def test_migration_installs_topology_tables_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` puts both tables + their indexes in place.

    Asserts via SQLite's schema inspector (the dialect-portable
    equivalent of ``\\d+`` against PG):

    * The ``graph_node`` and ``graph_edge`` tables exist with every
      documented column.
    * The unique index on ``graph_node`` (``graph_node_tenant_kind_name_idx``)
      and the three indexes on ``graph_edge``
      (``graph_edge_tenant_endpoints_kind_idx`` unique,
      ``graph_edge_tenant_from_idx``, ``graph_edge_tenant_to_idx``)
      are all present.

    PG-side verification (``\\d+`` against a real container) lives in
    the existing testcontainers suite that runs on CI.
    """
    sync_url, _ = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g91-schema.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node" in tables
            assert "graph_edge" in tables

            node_cols = {col["name"] for col in inspector.get_columns("graph_node")}
            expected_node_cols = {
                "id",
                "tenant_id",
                "kind",
                "name",
                "target_id",
                "properties",
                "discovered_by",
                "first_seen",
                "last_seen",
            }
            assert expected_node_cols <= node_cols, (
                f"Missing columns in graph_node: {expected_node_cols - node_cols}"
            )

            edge_cols = {col["name"] for col in inspector.get_columns("graph_edge")}
            expected_edge_cols = {
                "id",
                "tenant_id",
                "from_node_id",
                "to_node_id",
                "kind",
                "source",
                "properties",
                "discovered_by",
                "first_seen",
                "last_seen",
            }
            assert expected_edge_cols <= edge_cols, (
                f"Missing columns in graph_edge: {expected_edge_cols - edge_cols}"
            )

            node_indexes = {idx["name"] for idx in inspector.get_indexes("graph_node")}
            assert "graph_node_tenant_kind_name_idx" in node_indexes

            edge_indexes = {idx["name"] for idx in inspector.get_indexes("graph_edge")}
            assert "graph_edge_tenant_endpoints_kind_idx" in edge_indexes
            assert "graph_edge_tenant_from_idx" in edge_indexes
            assert "graph_edge_tenant_to_idx" in edge_indexes

            # last_seen must be nullable -- the soft-delete signal lives there.
            last_seen_col = next(
                col for col in inspector.get_columns("graph_node") if col["name"] == "last_seen"
            )
            assert last_seen_col["nullable"] is True

            # target_id must be nullable -- inner-graph nodes are not targets.
            target_id_col = next(
                col for col in inspector.get_columns("graph_node") if col["name"] == "target_id"
            )
            assert target_id_col["nullable"] is True

            # Foreign-key shape: graph_node points at tenant + targets.
            node_fks = inspector.get_foreign_keys("graph_node")
            referred_tables = {fk["referred_table"] for fk in node_fks}
            assert "tenant" in referred_tables
            assert "targets" in referred_tables

            # Foreign-key shape: graph_edge points at tenant + graph_node x2.
            edge_fks = inspector.get_foreign_keys("graph_edge")
            edge_referred_tables = [fk["referred_table"] for fk in edge_fks]
            assert "tenant" in edge_referred_tables
            # Two FKs reference graph_node (from + to).
            assert edge_referred_tables.count("graph_node") == 2
    finally:
        sync_eng.dispose()


def test_migration_upgrade_then_downgrade_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` -> ``alembic downgrade 0006`` -> ``upgrade head`` clean.

    Proves migration ``0007`` is fully reversible: after downgrading by
    exactly one revision (back to ``0006``, the parent_audit_id
    migration), the ``graph_node`` + ``graph_edge`` tables and indexes
    must be gone while the rest of the schema (``tenant``, ``targets``,
    ``audit_log``, ``documents``, ``operation_group``,
    ``endpoint_descriptor``) remains intact. Re-upgrading to head must
    restore everything cleanly.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g91-rev.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            assert "graph_node" in inspector.get_table_names()
            assert "graph_edge" in inspector.get_table_names()

        # Downgrade by exactly one revision (back to 0006).
        command.downgrade(cfg, "0006")

        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node" not in tables, "downgrade must drop graph_node"
            assert "graph_edge" not in tables, "downgrade must drop graph_edge"
            # Prior-migration tables must survive intact.
            assert "tenant" in tables
            assert "targets" in tables
            assert "audit_log" in tables
            assert "documents" in tables
            assert "operation_group" in tables
            assert "endpoint_descriptor" in tables

        # Re-upgrade -- must be idempotent from 0006 back to head.
        command.upgrade(cfg, "head")
        with sync_eng.connect() as conn:
            inspector = sa_inspect(conn)
            tables = set(inspector.get_table_names())
            assert "graph_node" in tables
            assert "graph_edge" in tables
    finally:
        sync_eng.dispose()


# ---------------------------------------------------------------------------
# G9.2-T1 (#593): closed 10-kind v0.2 vocabulary -- migration 0010 + enum
# ---------------------------------------------------------------------------


#: The six curated-only kinds migration ``0010`` adds on top of G9.1's
#: four auto-discoverable kinds. Used by the parametrised
#: post-migration accept tests below.
_G9_2_CURATED_KINDS: tuple[str, ...] = (
    "authenticates-via",
    "depends-on",
    "replicates-to",
    "backed-up-by",
    "routes-via",
    "policy-binds",
)


def test_graph_edge_kind_enum_has_exactly_ten_members() -> None:
    """:class:`GraphEdgeKind` carries the ten documented well-known members.

    Post-T1 #2534 the enum is the *well-known set*, not an enforced
    vocabulary -- it feeds docs tables, UI ``datalist`` suggestions,
    and error-message hints. The explicit member pin guards against a
    silent drop/rename that would desynchronise those surfaces from
    :file:`docs/architecture/topology.md`'s well-known table; widening
    the set is a deliberate docs + enum change (no migration needed).
    """
    assert len(GraphEdgeKind) == 10
    assert {k.value for k in GraphEdgeKind} == {
        "runs-on",
        "mounts",
        "routes-through",
        "belongs-to",
        "authenticates-via",
        "depends-on",
        "replicates-to",
        "backed-up-by",
        "routes-via",
        "policy-binds",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _G9_2_CURATED_KINDS)
async def test_graph_edge_post_migration_accepts_curated_kind(kind: str) -> None:
    """Post-migration: each of the six G9.2 curated kinds inserts without error.

    Migration ``0010`` widens ``ck_graph_edge_kind`` from G9.1's four
    auto-discoverable kinds to the ten-kind v0.2 set. The autouse
    ``_default_database_url`` fixture in :mod:`tests.conftest` runs
    ``alembic upgrade head`` before each test, so by the time this
    body runs the constraint is already widened. Inserting an edge
    with any of the six curated-only kinds must succeed; the row
    round-trips with the kind preserved.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        from_id, to_id = await _seed_two_nodes(session, tenant_id)
        edge_id = uuid.uuid4()
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=tenant_id,
                from_node_id=from_id,
                to_node_id=to_id,
                kind=kind,
                source="curated",
                discovered_by="annotator-test",
            )
        )
        # Must not raise -- the new vocabulary member is in the
        # post-0010 CHECK set.
        await session.commit()

    async with sessionmaker() as session:
        result = await session.execute(select(GraphEdge).where(GraphEdge.id == edge_id))
        row = result.scalar_one()

    assert row.kind == kind
    assert row.source == "curated"


def test_migration_0010_pre_upgrade_rejects_curated_kinds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pre-0010 (at revision 0009): the six curated kinds are rejected by the CHECK.

    Migrate up to ``0009`` (one revision before this task's migration),
    then attempt a direct INSERT for each of the six curated-only
    kinds. Every insert must raise :class:`IntegrityError` because
    revision ``0009``'s ``ck_graph_edge_kind`` still encodes the
    four-kind subset.

    Uses the sync :func:`sa_create_engine` rather than the async
    sessionmaker so the per-test SQLite file the autouse fixture
    created (which has already been upgraded to head) is not the one
    under test -- this test pins its own fresh SQLite file and stops
    the upgrade at ``0009`` so we can prove the pre-migration
    behaviour.
    """
    from alembic import command

    from meho_backplane.db.migrations import alembic_config

    db_path = tmp_path / "g92-rejects.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    # Stop at 0009 -- one revision before this task's migration.
    command.upgrade(cfg, "0009")

    sync_eng = sa_create_engine(sync_url)
    try:
        from sqlalchemy.exc import IntegrityError as SyncIntegrityError

        with sync_eng.begin() as conn:
            # PRAGMA fk on so the node FKs would trip if they were the
            # blocker -- we want to prove the kind CHECK is what
            # rejects, not a missing parent row.
            conn.execute(text("PRAGMA foreign_keys = ON"))

            tenant_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "slug": "pre-upgrade-tenant",
                    "name": "Pre Upgrade Tenant",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            from_id = str(uuid.uuid4())
            to_id = str(uuid.uuid4())
            for node_id, kind, name in (
                (from_id, "vm", "vm-pre-upgrade"),
                (to_id, "host", "host-pre-upgrade"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO graph_node "
                        "(id, tenant_id, kind, name, properties, "
                        "discovered_by, first_seen) "
                        "VALUES (:id, :tenant_id, :kind, :name, '{}', "
                        ":discovered_by, :first_seen)"
                    ),
                    {
                        "id": node_id,
                        "tenant_id": tenant_id,
                        "kind": kind,
                        "name": name,
                        "discovered_by": "vmware",
                        "first_seen": datetime.now(UTC).isoformat(),
                    },
                )

        for kind in _G9_2_CURATED_KINDS:
            with sync_eng.begin() as conn:
                stmt = text(
                    "INSERT INTO graph_edge "
                    "(id, tenant_id, from_node_id, to_node_id, kind, "
                    "source, properties, discovered_by, first_seen) "
                    "VALUES (:id, :tenant_id, :from_id, :to_id, :kind, "
                    ":source, '{}', :discovered_by, :first_seen)"
                )
                params = {
                    "id": str(uuid.uuid4()),
                    "tenant_id": tenant_id,
                    "from_id": from_id,
                    "to_id": to_id,
                    "kind": kind,
                    "source": "curated",
                    "discovered_by": "annotator-test",
                    "first_seen": datetime.now(UTC).isoformat(),
                }
                with pytest.raises(SyncIntegrityError):
                    conn.execute(stmt, params)
    finally:
        sync_eng.dispose()


def test_migration_0010_upgrade_downgrade_upgrade_cycle_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` -> ``downgrade 0009`` -> ``upgrade head`` is reversible.

    Proves migration ``0010`` is fully reversible against an empty
    ``graph_edge`` (no curated-kind rows that would block the
    downgrade pre-check). After the cycle, the constraint is back to
    the widened ten-kind tuple -- verified by attempting to insert a
    curated-kind row through the sync engine.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g92-cycle.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        # Downgrade by one revision (back to 0009).
        command.downgrade(cfg, "0009")
        # Re-upgrade to head -- must restore the widened constraint.
        command.upgrade(cfg, "head")

        # Prove the widened constraint is in effect by inserting a
        # curated-kind row directly through the sync engine.
        with sync_eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))

            tenant_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "slug": "cycle-tenant",
                    "name": "Cycle Tenant",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            from_id = str(uuid.uuid4())
            to_id = str(uuid.uuid4())
            for node_id, kind, name in (
                (from_id, "principal", "k8s-sa-cycle"),
                (to_id, "vault-role", "vault-role-cycle"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO graph_node "
                        "(id, tenant_id, kind, name, properties, "
                        "discovered_by, first_seen) "
                        "VALUES (:id, :tenant_id, :kind, :name, '{}', "
                        ":discovered_by, :first_seen)"
                    ),
                    {
                        "id": node_id,
                        "tenant_id": tenant_id,
                        "kind": kind,
                        "name": name,
                        "discovered_by": "curated",
                        "first_seen": datetime.now(UTC).isoformat(),
                    },
                )
            # Insert a row with one of the six new kinds; must succeed.
            conn.execute(
                text(
                    "INSERT INTO graph_edge "
                    "(id, tenant_id, from_node_id, to_node_id, kind, "
                    "source, properties, discovered_by, first_seen) "
                    "VALUES (:id, :tenant_id, :from_id, :to_id, "
                    "'authenticates-via', 'curated', '{}', "
                    ":discovered_by, :first_seen)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": tenant_id,
                    "from_id": from_id,
                    "to_id": to_id,
                    "discovered_by": "annotator-test",
                    "first_seen": datetime.now(UTC).isoformat(),
                },
            )
    finally:
        sync_eng.dispose()


def test_migration_0010_downgrade_refuses_when_curated_rows_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``downgrade 0009`` raises :class:`RuntimeError` if any curated-kind row exists.

    Acceptance criterion: narrowing the CHECK back to the four-kind
    subset must refuse loudly when rows whose ``kind`` is in the six
    removed kinds still exist -- silently violating the narrowed
    constraint (and crashing inside the DDL halfway through) is the
    failure mode the explicit pre-check guards against.

    The error message must name a row count and the affected kinds so
    operators can write the targeted cleanup before retrying the
    downgrade.
    """
    from alembic import command

    sync_url, cfg = _alembic_upgrade_against_fresh_sqlite(monkeypatch, tmp_path, "g92-refuse.db")

    sync_eng = sa_create_engine(sync_url)
    try:
        # Seed one row with each of two curated kinds so the error
        # message must aggregate counts by kind (not just a flat total).
        with sync_eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = ON"))

            tenant_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :created_at)"
                ),
                {
                    "id": tenant_id,
                    "slug": "refuse-tenant",
                    "name": "Refuse Tenant",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            from_id = str(uuid.uuid4())
            to_id = str(uuid.uuid4())
            for node_id, kind, name in (
                (from_id, "principal", "k8s-sa-refuse"),
                (to_id, "vault-role", "vault-role-refuse"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO graph_node "
                        "(id, tenant_id, kind, name, properties, "
                        "discovered_by, first_seen) "
                        "VALUES (:id, :tenant_id, :kind, :name, '{}', "
                        ":discovered_by, :first_seen)"
                    ),
                    {
                        "id": node_id,
                        "tenant_id": tenant_id,
                        "kind": kind,
                        "name": name,
                        "discovered_by": "curated",
                        "first_seen": datetime.now(UTC).isoformat(),
                    },
                )
            for kind in ("authenticates-via", "depends-on"):
                conn.execute(
                    text(
                        "INSERT INTO graph_edge "
                        "(id, tenant_id, from_node_id, to_node_id, "
                        "kind, source, properties, discovered_by, "
                        "first_seen) VALUES (:id, :tenant_id, :from_id, "
                        ":to_id, :kind, 'curated', '{}', "
                        ":discovered_by, :first_seen)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tenant_id": tenant_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "kind": kind,
                        "discovered_by": "annotator-test",
                        "first_seen": datetime.now(UTC).isoformat(),
                    },
                )

        # The downgrade must refuse and surface the row count + kinds.
        with pytest.raises(RuntimeError) as exc_info:
            command.downgrade(cfg, "0009")

        message = str(exc_info.value)
        assert "Cannot downgrade migration 0010" in message
        assert "authenticates-via" in message
        assert "depends-on" in message
        # Each kind has count=1 in the message ("kind=N" aggregation).
        assert "authenticates-via=1" in message
        assert "depends-on=1" in message
    finally:
        sync_eng.dispose()
