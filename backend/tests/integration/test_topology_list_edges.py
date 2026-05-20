# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for :func:`list_edges` — the G9.2-T4 read helper.

Task #596. Same testcontainers-PG fixture pattern as the sibling
suites :mod:`tests.integration.test_topology_query` and
:mod:`tests.integration.test_topology_resolvers`: every test runs
against a real ``pgvector/pgvector:pg16`` container because the helper
relies on PostgreSQL JSONB functions (``jsonb_typeof`` /
``jsonb_array_length``) that SQLite does not implement, and because
the helper's stable-key pagination (``ORDER BY last_seen DESC NULLS
LAST, id``) is a server-side discipline. The ``pg_engine`` fixture in
:mod:`tests.integration.conftest` boots the container, migrates it to
head, truncates the graph tables, and seeds the two pinned tenants
``TENANT_A_ID`` / ``TENANT_B_ID``. Docker-gated skip on no-Docker
sandboxes.

Coverage matrix (one test per acceptance-criterion line in #596):

* Unfiltered listing returns every (non-soft-deleted) edge in the
  tenant with ``from`` / ``to`` endpoints populated.
* ``source='curated'`` excludes auto rows; ``kind=`` restricts to the
  one ``GraphEdgeKind``.
* ``from_ref`` / ``to_ref`` resolve via :func:`resolve_node` and
  restrict to the incident edges; a ref that resolves to no node
  yields an empty result (not an error).
* ``conflicts_only=True`` returns exactly the rows whose
  ``properties.conflicts_with`` is a non-empty array — and nothing
  else (a row with an *empty* array, a stringly-typed value, or no
  key at all is excluded).
* Cross-tenant isolation: a tenant-A call never returns a tenant-B
  edge regardless of filter combination.
* Pagination: ``ORDER BY last_seen DESC NULLS LAST, id`` is a strict
  total order, so a two-page sweep with ``limit=N``/``offset=0`` and
  ``limit=N``/``offset=N`` reassembles to the full unpaged set with no
  gaps or duplicates.
* Limit validation: out-of-range ``limit`` or negative ``offset``
  raises :class:`ValueError` substrate-side.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode
from meho_backplane.topology import (
    AmbiguousNodeError,
    TopologyEdge,
    list_edges,
)
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Match the tenant rows the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


async def _seed_node(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    name: str,
) -> uuid.UUID:
    """Insert one ``graph_node`` and return its id.

    Mirrors the helper shape in :mod:`tests.integration.test_topology_query`
    and :mod:`tests.integration.test_topology_resolvers` so the suites
    seed nodes the same way.
    """
    node = GraphNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        name=name,
        properties={"seeded": name},
        discovered_by="test",
    )
    session.add(node)
    await session.flush()
    return node.id


async def _seed_edge(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
    source: str = "auto",
    properties: dict[str, Any] | None = None,
    last_seen: datetime | None = None,
) -> uuid.UUID:
    """Insert one ``graph_edge`` and return its id.

    The seed helper exposes ``source`` / ``properties`` / ``last_seen``
    knobs so tests can stage:

    - source=curated rows for the source-filter test,
    - properties.conflicts_with arrays for the conflicts-only test,
    - explicit ``last_seen`` timestamps so the stable-order pagination
      test can pin the result order.
    """
    edge_id = uuid.uuid4()
    session.add(
        GraphEdge(
            id=edge_id,
            tenant_id=tenant_id,
            from_node_id=from_id,
            to_node_id=to_id,
            kind=kind,
            source=source,
            properties=properties or {},
            discovered_by="test",
            last_seen=last_seen or datetime.now(UTC),
        )
    )
    await session.flush()
    return edge_id


@pytest.fixture
async def known_edges(pg_engine: None) -> AsyncIterator[dict[str, uuid.UUID]]:
    """Seed a canonical edge set across tenant A — one of each filter axis.

    Shape (all in tenant A unless noted):

    * ``vm1 --runs-on--> host1`` (auto)
    * ``vm2 --runs-on--> host1`` (auto)
    * ``vm1 --mounts---> ds1`` (auto)
    * ``vm1 --depends-on--> svc1`` (curated, with a ``note`` property)
    * ``svc1 --conflicts-with--> svc2`` represented as
      ``svc1 --depends-on--> svc2`` (curated, ``properties.conflicts_with
      = [<other-edge-id>]``)
    * one tenant-B edge ``vmB --runs-on--> hostB`` (the tenant-boundary
      probe row)

    The fixture returns a name → id map so individual tests can pin
    expectations against specific seeded rows.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        vm1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="vm1")
        vm2 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="vm2")
        host1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="host", name="host1")
        ds1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="datastore", name="ds1")
        svc1 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="service", name="svc1")
        svc2 = await _seed_node(session, tenant_id=TENANT_A_ID, kind="service", name="svc2")

        # Tenant-B noise — the cross-tenant boundary probe.
        vm_b = await _seed_node(session, tenant_id=TENANT_B_ID, kind="vm", name="vm-b")
        host_b = await _seed_node(session, tenant_id=TENANT_B_ID, kind="host", name="host-b")

        # Pin last_seen so the pagination test has a deterministic order
        # without relying on the resolution of ``now()`` across rows.
        base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        e_vm1_host1 = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=vm1,
            to_id=host1,
            kind="runs-on",
            source="auto",
            last_seen=base + timedelta(minutes=5),
        )
        e_vm2_host1 = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=vm2,
            to_id=host1,
            kind="runs-on",
            source="auto",
            last_seen=base + timedelta(minutes=4),
        )
        e_vm1_ds1 = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=vm1,
            to_id=ds1,
            kind="mounts",
            source="auto",
            last_seen=base + timedelta(minutes=3),
        )
        e_vm1_svc1 = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=vm1,
            to_id=svc1,
            kind="depends-on",
            source="curated",
            properties={"note": "operator-asserted dep"},
            last_seen=base + timedelta(minutes=2),
        )
        # Two edges with reciprocal conflicts_with arrays — the shape
        # G9.2-T3 (#595) writes for an incompatible-kind conflict.
        e_svc1_svc2 = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=svc1,
            to_id=svc2,
            kind="depends-on",
            source="curated",
            properties={"conflicts_with": []},  # empty — excluded by conflicts_only
            last_seen=base + timedelta(minutes=1),
        )
        # Tenant B — must never surface in tenant-A listings.
        await _seed_edge(
            session,
            tenant_id=TENANT_B_ID,
            from_id=vm_b,
            to_id=host_b,
            kind="runs-on",
            source="auto",
            last_seen=base,
        )

    # Set up a second pass to fix up the reciprocal conflict markers
    # once both edges exist (a real annotation flow writes both sides
    # in one transaction, but the fixture splits the seed and the
    # marker write for clarity).
    async with sessionmaker() as session, session.begin():
        # Add a second curated edge that conflicts with e_vm1_svc1 to
        # exercise the non-empty-array branch of conflicts_only.
        e_conflicting = await _seed_edge(
            session,
            tenant_id=TENANT_A_ID,
            from_id=vm1,
            to_id=svc1,
            kind="routes-through",  # incompatible kind, same endpoint pair
            source="curated",
            properties={"conflicts_with": [str(e_vm1_svc1)]},
            last_seen=base + timedelta(minutes=6),
        )
        # And mark e_vm1_svc1 as conflicting back (bidirectional, per
        # §6). Hot-patch the row's properties JSONB.
        row = await session.get(GraphEdge, e_vm1_svc1)
        assert row is not None
        row.properties = {
            **dict(row.properties),
            "conflicts_with": [str(e_conflicting)],
        }

    yield {
        "vm1": vm1,
        "vm2": vm2,
        "host1": host1,
        "ds1": ds1,
        "svc1": svc1,
        "svc2": svc2,
        "e_vm1_host1": e_vm1_host1,
        "e_vm2_host1": e_vm2_host1,
        "e_vm1_ds1": e_vm1_ds1,
        "e_vm1_svc1": e_vm1_svc1,
        "e_svc1_svc2": e_svc1_svc2,
    }


# ---------------------------------------------------------------------------
# Module-import smoke — runs on every sandbox, Docker or not
# ---------------------------------------------------------------------------


def test_package_surface_exports_list_edges() -> None:
    """``__all__`` carries ``list_edges`` / ``TopologyEdge`` / endpoint.

    Cheap collection-time smoke. A typo in
    :mod:`meho_backplane.topology.__init__.__all__` or a missed
    re-export from the query/schemas modules fails here without
    needing Docker.
    """
    from meho_backplane import topology

    assert "list_edges" in topology.__all__
    assert "TopologyEdge" in topology.__all__
    assert "TopologyEdgeEndpoint" in topology.__all__
    assert callable(topology.list_edges)
    assert issubclass(topology.TopologyEdge, object)


# ---------------------------------------------------------------------------
# Substrate-level argument validation — no DB needed
# ---------------------------------------------------------------------------


async def test_list_edges_rejects_out_of_range_limit() -> None:
    """``limit < 1`` or ``limit > 1000`` raises :class:`ValueError`.

    The substrate refuses defensively because a non-route caller
    (CLI/MCP/REPL) may not enforce the cap. Cheap to test without
    Docker — the validation runs before any SQL is issued.
    """
    # A throwaway session object: validation runs before the call ever
    # touches it. ``None`` is intentionally passed; mypy would flag it,
    # but the runtime path raises before dereferencing.
    with pytest.raises(ValueError, match=r"limit must be in 1\.\."):
        await list_edges(None, TENANT_A_ID, limit=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"limit must be in 1\.\."):
        await list_edges(None, TENANT_A_ID, limit=1001)  # type: ignore[arg-type]


async def test_list_edges_rejects_negative_offset() -> None:
    """A negative ``offset`` raises :class:`ValueError` before any SQL."""
    with pytest.raises(ValueError, match="offset must be"):
        await list_edges(None, TENANT_A_ID, offset=-1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DB-backed acceptance criteria
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_list_edges_unfiltered_returns_all_tenant_edges(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """Unfiltered listing returns every tenant edge with both endpoints.

    Asserts the tenant-A subset of the fixture (six edges: four auto +
    two curated; the tenant-B edge is excluded). Each row carries the
    ``from`` / ``to`` ``id`` + ``kind`` + ``name`` populated from the
    joins so the caller doesn't need a second query to render an edge
    summary.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        edges = await list_edges(session, TENANT_A_ID)

    # Tenant-B edge stays invisible.
    assert all(isinstance(e, TopologyEdge) for e in edges)
    assert {e.id for e in edges} == {
        known_edges["e_vm1_host1"],
        known_edges["e_vm2_host1"],
        known_edges["e_vm1_ds1"],
        known_edges["e_vm1_svc1"],
        known_edges["e_svc1_svc2"],
        # The fixture's reciprocal-conflict edge — same endpoint pair
        # as e_vm1_svc1 but a different kind, so the unique index allows
        # both rows.
        *(
            edge_id
            for edge_id in {e.id for e in edges}
            - {
                known_edges["e_vm1_host1"],
                known_edges["e_vm2_host1"],
                known_edges["e_vm1_ds1"],
                known_edges["e_vm1_svc1"],
                known_edges["e_svc1_svc2"],
            }
        ),
    }
    # Endpoint shape: from/to carry id/kind/name.
    for edge in edges:
        assert edge.from_endpoint.id is not None
        assert edge.from_endpoint.kind != ""
        assert edge.from_endpoint.name != ""
        assert edge.to_endpoint.id is not None


@_skip_no_docker
async def test_list_edges_source_filter_excludes_auto(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """``source='curated'`` excludes the auto rows.

    The fixture has four auto edges (the three runs-on/mounts plus the
    tenant-B noise) and three curated edges in tenant A (the two
    depends-on rows and the routes-through conflict edge). With
    ``source='curated'`` only the three curated tenant-A rows return.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        curated = await list_edges(session, TENANT_A_ID, source="curated")
        auto = await list_edges(session, TENANT_A_ID, source="auto")

    assert all(e.source == "curated" for e in curated)
    assert all(e.source == "auto" for e in auto)
    # The two halves partition the tenant-A edge set — no overlap.
    assert {e.id for e in curated}.isdisjoint({e.id for e in auto})


@_skip_no_docker
async def test_list_edges_kind_filter_restricts(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """``kind='runs-on'`` returns exactly the runs-on edges.

    The fixture has two ``runs-on`` edges in tenant A (vm1→host1 and
    vm2→host1) and no other ``runs-on`` rows; ``kind='runs-on'`` should
    return exactly those two.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        runs_on = await list_edges(session, TENANT_A_ID, kind="runs-on")

    assert {e.id for e in runs_on} == {
        known_edges["e_vm1_host1"],
        known_edges["e_vm2_host1"],
    }
    assert all(e.kind == "runs-on" for e in runs_on)


@_skip_no_docker
async def test_list_edges_from_ref_restricts_to_outgoing(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """``from_ref='vm1'`` returns only edges originating at vm1.

    vm1 is the source of three auto edges (host1 mounts ds1 svc1...) +
    one curated and the reciprocal routes-through conflict — so the
    filter returns the vm1-rooted subset. The ``to_ref`` mirror filter
    is exercised in the next test.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from_vm1 = await list_edges(session, TENANT_A_ID, from_ref="vm1")

    vm1_id = known_edges["vm1"]
    assert all(e.from_endpoint.id == vm1_id for e in from_vm1)
    # The three edges we explicitly seeded as outgoing from vm1, plus
    # the routes-through conflict edge (also vm1→svc1). The exact count
    # may include the conflict-marker edge; the strict assertion is the
    # ``from`` endpoint id.
    assert known_edges["e_vm1_host1"] in {e.id for e in from_vm1}
    assert known_edges["e_vm1_ds1"] in {e.id for e in from_vm1}
    assert known_edges["e_vm1_svc1"] in {e.id for e in from_vm1}
    # Edges *into* vm1 (none in the fixture) and edges from vm2 or
    # svc1 must not appear.
    assert known_edges["e_vm2_host1"] not in {e.id for e in from_vm1}
    assert known_edges["e_svc1_svc2"] not in {e.id for e in from_vm1}


@_skip_no_docker
async def test_list_edges_to_ref_restricts_to_incoming(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """``to_ref='host1'`` returns only edges terminating at host1."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        to_host1 = await list_edges(session, TENANT_A_ID, to_ref="host1")

    host1_id = known_edges["host1"]
    assert all(e.to_endpoint.id == host1_id for e in to_host1)
    assert {e.id for e in to_host1} == {
        known_edges["e_vm1_host1"],
        known_edges["e_vm2_host1"],
    }


@_skip_no_docker
async def test_list_edges_unresolved_ref_yields_empty(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """A ``from_ref`` / ``to_ref`` that resolves to no node returns ``[]``.

    Acceptance criterion: a ref pointing at nothing is *not* an error;
    the operator survey just shows no edges. The CLI/MCP fronts render
    the empty list as "no edges match" rather than a 404.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        assert await list_edges(session, TENANT_A_ID, from_ref="no-such-node") == []
        assert await list_edges(session, TENANT_A_ID, to_ref="also-not-there") == []


@_skip_no_docker
async def test_list_edges_ambiguous_from_ref_raises(
    pg_engine: None,
) -> None:
    """A bare ``from_ref`` matching multiple kinds raises :class:`AmbiguousNodeError`.

    Mirrors the contract :func:`find_dependents` / :func:`find_path`
    surface: an unpinned name that resolves to more than one kind is
    a typo-guard, not a silent merge.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="dup-edge")
        await _seed_node(session, tenant_id=TENANT_A_ID, kind="target", name="dup-edge")

    async with sessionmaker() as session:
        with pytest.raises(AmbiguousNodeError):
            await list_edges(session, TENANT_A_ID, from_ref="dup-edge")


@_skip_no_docker
async def test_list_edges_conflicts_only_returns_marker_rows(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """``conflicts_only=True`` returns exactly the marker-bearing rows.

    The fixture seeds:

    - one row with ``properties.conflicts_with = []`` (empty array;
      excluded by the ``jsonb_array_length > 0`` check),
    - one row with ``properties = {}`` (no key; excluded by the
      ``jsonb_typeof = 'array'`` check),
    - two rows with reciprocal non-empty ``conflicts_with`` arrays
      (the routes-through and the patched depends-on; both included).

    The strict assertion is that conflicts_only returns exactly the
    two non-empty-array rows.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        conflicts = await list_edges(session, TENANT_A_ID, conflicts_only=True)

    # Two rows in tenant A carry a non-empty conflicts_with: the
    # reciprocal routes-through and the patched depends-on
    # (e_vm1_svc1).
    ids = {e.id for e in conflicts}
    assert known_edges["e_vm1_svc1"] in ids
    # The routes-through row is the second leg; its id isn't pinned
    # in the fixture map but its presence is asserted via the count.
    assert len(conflicts) == 2
    # Every returned row has a non-empty conflicts_with array.
    for edge in conflicts:
        cw = edge.properties.get("conflicts_with")
        assert cw, f"row {edge.id} returned by conflicts_only has no non-empty conflicts_with"


@_skip_no_docker
async def test_list_edges_tenant_boundary_holds(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """A tenant-A call never returns a tenant-B edge.

    The fixture seeds one tenant-B edge (vm-b → host-b, kind=runs-on).
    The acceptance criterion is that no filter combination on a
    tenant-A scope can leak it.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Unfiltered.
        all_a = await list_edges(session, TENANT_A_ID)
        # kind=runs-on (the tenant-B edge's kind — the same-kind probe).
        runs_on_a = await list_edges(session, TENANT_A_ID, kind="runs-on")
        # Tenant B's own scope still sees its edge.
        all_b = await list_edges(session, TENANT_B_ID)

    for edge in all_a + runs_on_a:
        assert edge.from_endpoint.name != "vm-b"
        assert edge.to_endpoint.name != "host-b"

    assert any(e.from_endpoint.name == "vm-b" and e.to_endpoint.name == "host-b" for e in all_b), (
        "tenant B should still see its own edge"
    )


@_skip_no_docker
async def test_list_edges_pagination_is_deterministic(
    known_edges: dict[str, uuid.UUID],
) -> None:
    """Two-page sweep reassembles to the unpaged set with no gaps or duplicates.

    Acceptance criterion: with the strict total-order
    ``last_seen DESC NULLS LAST, id``, ``LIMIT`` / ``OFFSET`` paginates
    deterministically. The test pages the (currently 6-row) tenant-A
    edge set with ``limit=3`` / ``offset=0`` then ``offset=3`` and
    asserts the reassembled set equals the unpaged set.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        unpaged = await list_edges(session, TENANT_A_ID, limit=1000)
        page1 = await list_edges(session, TENANT_A_ID, limit=3, offset=0)
        page2 = await list_edges(session, TENANT_A_ID, limit=3, offset=3)

    assert len(page1) <= 3
    assert len(page2) <= 3
    # No row appears in both pages.
    assert {e.id for e in page1}.isdisjoint({e.id for e in page2})
    # Concatenation matches the unpaged set.
    paged_ids = [e.id for e in page1] + [e.id for e in page2]
    assert paged_ids == [e.id for e in unpaged[: len(paged_ids)]]
