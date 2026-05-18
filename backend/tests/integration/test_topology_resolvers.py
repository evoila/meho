# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for :func:`resolve_node` and the relocated errors.

Task #594 (G9.2-T2) acceptance suite. Mirrors the shape of
:mod:`tests.integration.test_topology_query` — every test runs against
a real ``pgvector/pgvector:pg16`` container (the resolver hits real
asyncpg + the ``graph_node_tenant_kind_name_idx`` unique index, not
SQLite). The ``pg_engine`` fixture in
:mod:`tests.integration.conftest` boots the container, migrates it to
head, truncates the graph tables, and seeds the two pinned tenants
``TENANT_A_ID`` / ``TENANT_B_ID``. Docker-gated skip on no-Docker
sandboxes — same idiom :mod:`tests.integration.test_topology_query`
documents.

Coverage matrix (one test per acceptance-criterion line in #594):

* A bare name with one match returns the row.
* A bare name with two kinds raises :class:`AmbiguousNodeError`
  whose ``kinds`` lists both candidates.
* A pinned ``kind`` returns the unambiguous row.
* No match raises :class:`NodeNotFoundError`.
* A name seeded only in another tenant raises
  :class:`NodeNotFoundError` when resolved from the first tenant
  (cross-tenant references never resolve to the other tenant).
* :func:`resolve_node` works for non-target nodes
  (``target_id IS NULL`` — the annotation flow's canonical case for
  ``vault-role`` / ``keycloak-realm`` rows).
* The pre-existing
  ``from meho_backplane.topology.query import AmbiguousNodeError``
  back-compat import surface keeps resolving to the same class as
  the new resolver module exports.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode
from meho_backplane.topology import (
    AmbiguousNodeError,
    NodeNotFoundError,
    resolve_node,
)
from meho_backplane.topology.query import AmbiguousNodeError as _QueryAmbig
from meho_backplane.topology.resolvers import AmbiguousNodeError as _ResolverAmbig
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
    target_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one ``graph_node`` and return its id.

    Mirrors the helper in :mod:`tests.integration.test_topology_query`
    but adds an explicit ``target_id`` knob so a test can seed a
    non-target node (the resolver's annotation use case requires the
    ``target_id IS NULL`` path to work).
    """
    node = GraphNode(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind,
        name=name,
        target_id=target_id,
        properties={"seeded": name},
        discovered_by="test",
    )
    session.add(node)
    await session.flush()
    return node.id


# ---------------------------------------------------------------------------
# Module-import smoke — runs on every sandbox, Docker or not
# ---------------------------------------------------------------------------


def test_back_compat_import_resolves_to_resolver_class() -> None:
    """Both import paths point to the same :class:`AmbiguousNodeError`.

    Pre-G9.2 callers import from
    :mod:`meho_backplane.topology.query`; new callers (and the package
    surface) re-export from :mod:`meho_backplane.topology.resolvers`.
    The acceptance criterion "no broken AmbiguousNodeError import after
    the relocation" is enforced by `_QueryAmbig is _ResolverAmbig` —
    a re-bind to a *different* class would let pre-existing ``except
    AmbiguousNodeError`` blocks miss the relocated raise and silently
    propagate as a 500. The package-surface alias
    (``from meho_backplane.topology import AmbiguousNodeError``) is
    checked alongside so the package surface cannot drift either.
    """
    assert _QueryAmbig is _ResolverAmbig
    assert AmbiguousNodeError is _ResolverAmbig


def test_package_surface_exports_resolver_symbols() -> None:
    """``__all__`` carries ``resolve_node`` / both errors; types are sane.

    Cheap collection-time smoke that runs on no-Docker sandboxes —
    same idiom :mod:`tests.integration.test_topology_query` keeps. A
    typo in ``meho_backplane.topology.__init__.__all__`` or a class
    that stops subclassing :class:`ValueError` fails here first.
    """
    from meho_backplane import topology

    assert "resolve_node" in topology.__all__
    assert "AmbiguousNodeError" in topology.__all__
    assert "NodeNotFoundError" in topology.__all__
    assert callable(topology.resolve_node)
    assert issubclass(topology.AmbiguousNodeError, ValueError)
    assert issubclass(topology.NodeNotFoundError, ValueError)


# ---------------------------------------------------------------------------
# DB-backed acceptance criteria
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_resolve_node_returns_unique_row_for_bare_name(
    pg_engine: None,
) -> None:
    """Bare name with exactly one match returns the row.

    The simplest happy path — the name exists once in the tenant, no
    ``kind`` is needed for disambiguation, the resolver fetches and
    returns the matching :class:`GraphNode`.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        node_id = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="solo")

    async with sessionmaker() as session:
        node = await resolve_node(session, TENANT_A_ID, "solo")

    assert node.id == node_id
    assert node.name == "solo"
    assert node.kind == "vm"
    assert node.tenant_id == TENANT_A_ID


@_skip_no_docker
async def test_resolve_node_ambiguous_lists_both_candidate_kinds(
    pg_engine: None,
) -> None:
    """Bare name with two kinds raises :class:`AmbiguousNodeError`.

    The error message must list both candidate kinds so the caller
    can re-issue with the right ``kind=`` argument without a second
    round trip. ``sorted(.kinds)`` is the contract documented on the
    error class.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="dup")
        await _seed_node(session, tenant_id=TENANT_A_ID, kind="target", name="dup")

    async with sessionmaker() as session:
        with pytest.raises(AmbiguousNodeError) as excinfo:
            await resolve_node(session, TENANT_A_ID, "dup")

    assert excinfo.value.name == "dup"
    assert sorted(excinfo.value.kinds) == ["target", "vm"]


@_skip_no_docker
async def test_resolve_node_pinned_kind_returns_unambiguous_row(
    pg_engine: None,
) -> None:
    """Same name across two kinds; a pinned ``kind`` picks the right row.

    Mirror of the ambiguity test — the duplicate name does not raise
    once ``kind=`` is supplied, because the
    ``graph_node_tenant_kind_name_idx`` unique index guarantees at
    most one row.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        vm_id = await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="dup")
        target_id = await _seed_node(session, tenant_id=TENANT_A_ID, kind="target", name="dup")

    async with sessionmaker() as session:
        vm_node = await resolve_node(session, TENANT_A_ID, "dup", kind="vm")
        target_node = await resolve_node(session, TENANT_A_ID, "dup", kind="target")

    assert vm_node.id == vm_id
    assert vm_node.kind == "vm"
    assert target_node.id == target_id
    assert target_node.kind == "target"


@_skip_no_docker
async def test_resolve_node_raises_not_found_when_absent(
    pg_engine: None,
) -> None:
    """No match anywhere raises :class:`NodeNotFoundError`.

    Bare-name absent and kind-pinned absent both surface as the same
    error class — the resolver does not split "name exists in some
    other kind" vs "name does not exist at all" because both are
    a no-match for the caller's intent.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        # Seed one unrelated node so the table is not empty (a more
        # realistic miss than "no rows at all"). ``ghost`` is what
        # the test will look for, ``other`` is the noise.
        await _seed_node(session, tenant_id=TENANT_A_ID, kind="vm", name="other")

    async with sessionmaker() as session:
        with pytest.raises(NodeNotFoundError) as excinfo_bare:
            await resolve_node(session, TENANT_A_ID, "ghost")
        with pytest.raises(NodeNotFoundError) as excinfo_pinned:
            await resolve_node(session, TENANT_A_ID, "other", kind="target")

    assert excinfo_bare.value.name == "ghost"
    assert excinfo_bare.value.kind is None
    assert excinfo_pinned.value.name == "other"
    assert excinfo_pinned.value.kind == "target"


@_skip_no_docker
async def test_resolve_node_tenant_boundary_holds(pg_engine: None) -> None:
    """A name seeded only in tenant B raises :class:`NodeNotFoundError`.

    Acceptance criterion: cross-tenant references resolve to "not
    found", never to the other tenant's node. This is the substrate
    the §11 tenant-boundary test in Initiative #364 depends on (a
    tenant-A admin trying to annotate a tenant-B node hits 404, not
    a silent cross-tenant write).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        # tenant B owns "cross-tenant" — there is no row by that name
        # in tenant A.
        b_node_id = await _seed_node(session, tenant_id=TENANT_B_ID, kind="vm", name="cross-tenant")

    # Tenant A cannot see it — bare-name and pinned-kind both miss.
    async with sessionmaker() as session:
        with pytest.raises(NodeNotFoundError):
            await resolve_node(session, TENANT_A_ID, "cross-tenant")
        with pytest.raises(NodeNotFoundError):
            await resolve_node(session, TENANT_A_ID, "cross-tenant", kind="vm")

    # Tenant B still sees it — the boundary is symmetric, not a
    # global blackhole. (The acceptance criterion focuses on the
    # cross-tenant raise; this same-tenant sanity check rules out
    # "the seed never landed" as an alternative explanation.)
    async with sessionmaker() as session:
        node = await resolve_node(session, TENANT_B_ID, "cross-tenant")
    assert node.id == b_node_id


@_skip_no_docker
async def test_resolve_node_works_for_non_target_nodes(
    pg_engine: None,
) -> None:
    """``target_id IS NULL`` nodes resolve normally.

    Acceptance criterion. The annotation flow (G9.2-T3) routinely
    references nodes that are not managed targets — a ``vault-role``
    or ``keycloak-realm`` row is exactly this shape — and the
    resolver must not assume ``target_id`` is populated. The seeded
    row's ``target_id`` is explicitly ``None``; the resolver returns
    it the same way it returns a target-backed row.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        node_id = await _seed_node(
            session,
            tenant_id=TENANT_A_ID,
            kind="vm",
            name="non-target",
            target_id=None,
        )

    async with sessionmaker() as session:
        node = await resolve_node(session, TENANT_A_ID, "non-target")

    assert node.id == node_id
    assert node.target_id is None
