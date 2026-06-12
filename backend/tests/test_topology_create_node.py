# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G0.9.1-T6 ``create_or_get_node`` service.

Coverage matrix (Task #778 acceptance criteria — the service-level
half; the MCP-level surface is covered in
:mod:`tests.test_mcp_tools_topology_annotate`):

* **Fresh insert** — first call on a clean tenant inserts one
  ``source='curated'``-shaped row (``discovered_by=operator.sub``) with
  the manual-seed property bag (``note``, ``evidence_url``,
  ``seeded_by``, ``seeded_at``).
* **Idempotent re-seed** — a second call with the same
  ``(kind, name)`` returns ``was_created=False`` and refreshes the
  existing row's ``last_seen`` + manual-seed properties without
  duplicating.
* **Promotes auto-discovered rows** — a re-seed over an existing
  ``discovered_by='vmware'``-style auto row keeps the row but
  promotes ``discovered_by`` to the operator (matches
  :func:`annotate_edge`'s auto→curated promotion).
* **Kind validation** — a non-vocabulary ``kind`` raises
  :class:`InvalidNodeKindError` *before* any DB write.
* **Tenant boundary** — a name seeded in tenant-B is invisible to a
  tenant-A operator; their create_or_get inserts a fresh row in
  tenant-A and does not collide with the tenant-B row.
* **Audit + broadcast** — every call writes exactly one
  ``audit_log`` row (``op_id='topology.create_node'``,
  ``method='CREATE_NODE'``, ``op_class='write'``) and publishes
  exactly one broadcast event. Publish failure is swallowed
  (fail-open).
* **Bootstrap → annotate flow** — a fresh tenant can seed two nodes
  via :func:`create_or_get_node` and then annotate an edge between
  them (the issue's "from-zero-to-annotated" acceptance criterion at
  the substrate layer; the MCP-level repeat lives in the dispatcher
  test).

Runs against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest` — same
shape :mod:`tests.test_topology_annotate` uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    GraphHistoryChangeKind,
    GraphNode,
    GraphNodeHistory,
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.topology import (
    InvalidNodeKindError,
    NodeRef,
    annotate_edge,
    create_or_get_node,
)
from meho_backplane.topology.query import query_history, query_timeline

_PUBLISH = "meho_backplane.topology.nodes.publish_event"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


async def _seed_tenant(slug: str = "rdc-internal") -> uuid.UUID:
    """Return the tenant row's id, inserting it on the first call.

    The look-up-then-insert shape is load-bearing: migration ``0018``
    seeds the ``rdc-internal`` tenant into the per-worker schema
    template (:func:`tests.conftest._schema_template_db`), so a plain
    ``session.add(Tenant(slug='rdc-internal', ...))`` would trip
    ``UNIQUE constraint failed: tenant.slug``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == slug),
        )
        if existing is not None:
            return existing
        tenant_id = uuid.uuid4()
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


def _operator(tenant_id: uuid.UUID, sub: str = "op-1") -> Operator:
    return Operator(
        sub=sub,
        name="Op One",
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# Happy path: insert + idempotent re-seed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_node_inserts_fresh_row() -> None:
    """First call on a clean tenant inserts one ``curated``-shape row."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="seeded from INVENTORY.md L42",
                evidence_url="https://example.test/inv#L42",
            )

    assert result.was_created is True
    assert result.node.kind == "vault-role"
    assert result.node.name == "rdc-vault"
    assert result.node.discovered_by == "op-1"
    assert result.node.target_id is None
    assert result.node.properties["note"] == "seeded from INVENTORY.md L42"
    assert result.node.properties["evidence_url"] == "https://example.test/inv#L42"
    assert result.node.properties["seeded_by"] == "op-1"
    assert "seeded_at" in result.node.properties

    # Exactly one row landed in the tenant.
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_create_node_is_idempotent_on_repeat() -> None:
    """A second call with the same (kind, name) updates, not duplicates."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            first = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="first",
            )
        async with sessionmaker() as session:
            second = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="second",
            )

    assert first.was_created is True
    assert second.was_created is False
    assert first.node.id == second.node.id

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # The manual-seed note refreshed to the second call's value.
    assert rows[0].properties["note"] == "second"


@pytest.mark.asyncio
async def test_create_node_over_auto_promotes_discovered_by() -> None:
    """Seeding over an existing auto-discovered row promotes ``discovered_by``.

    Mirrors the :func:`annotate_edge` auto→curated promotion shape: an
    operator manually re-seeding a node the refresh service first
    discovered takes ownership going forward. The row keeps its
    identity ``(tenant, kind, name)`` (refresh will still find and
    update it), but the audit trail credits the operator.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()
    # Pre-existing auto-discovered row.
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                kind="vm",
                name="legacy-vm",
                target_id=None,
                properties={"status": "running"},
                discovered_by="vmware",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
        await session.commit()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vm",
                name="legacy-vm",
                note="taking ownership for cross-system depends-on",
            )

    assert result.was_created is False
    assert result.node.discovered_by == "op-1"
    # Auto-discovered keys are preserved alongside the manual-seed bag.
    assert result.node.properties["status"] == "running"
    assert result.node.properties["note"] == "taking ownership for cross-system depends-on"
    assert result.node.properties["seeded_by"] == "op-1"


# ---------------------------------------------------------------------------
# Kind validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_node_rejects_unknown_kind_before_db_write() -> None:
    """A non-vocabulary kind raises ``InvalidNodeKindError`` pre-DB."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()) as publish_mock:
        async with sessionmaker() as session:
            with pytest.raises(InvalidNodeKindError) as excinfo:
                await create_or_get_node(
                    session,
                    _operator(tenant_id),
                    kind="quantum-blob",
                    name="entangled",
                )

    assert "quantum-blob" in str(excinfo.value)
    # Vocabulary list is echoed for the operator to recover from.
    assert "vault-role" in str(excinfo.value)

    # No row landed, no broadcast emitted.
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert rows == []
    publish_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Audit + broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_node_writes_one_audit_row_and_one_broadcast() -> None:
    """Exactly one ``audit_log`` row + one broadcast event per call."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()) as publish_mock:
        async with sessionmaker() as session:
            await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="principal",
                name="k8s-sa-foo",
                evidence_url="https://example.test/inv#sa-foo",
            )

    async with sessionmaker() as session:
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .all()
        )

    assert len(audits) == 1
    row = audits[0]
    assert row.method == "CREATE_NODE"
    assert row.path == "topology.create_node"
    assert row.status_code == 200
    payload = row.payload
    assert payload["op_id"] == "topology.create_node"
    assert payload["op_class"] == "write"
    assert payload["kind"] == "principal"
    assert payload["name"] == "k8s-sa-foo"
    assert payload["was_created"] is True
    assert payload["evidence_url"] == "https://example.test/inv#sa-foo"

    # Exactly one broadcast emission with the same audit_id pre-allocated.
    assert publish_mock.await_count == 1
    event = publish_mock.await_args.args[0]
    assert event.op_id == "topology.create_node"
    assert event.op_class == "write"
    assert event.audit_id == row.id
    assert event.target_name == "k8s-sa-foo"


@pytest.mark.asyncio
async def test_create_node_broadcast_is_fail_open() -> None:
    """A broadcast publish exception is logged, never raised."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock(side_effect=RuntimeError("stream down"))):
        async with sessionmaker() as session:
            result = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
            )

    # Row still landed despite the publisher failure.
    assert result.was_created is True
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GraphNode).where(GraphNode.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_node_does_not_collide_across_tenants() -> None:
    """A name seeded in tenant-B is invisible to tenant-A's create_or_get.

    The unique key is ``(tenant_id, kind, name)``, so the same triple
    can land in two tenants as two independent rows. Verifies the
    tenant-isolation invariant directly.
    """
    tenant_a = await _seed_tenant(slug="tenant-a")
    tenant_b = await _seed_tenant(slug="tenant-b")
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            await create_or_get_node(
                session,
                _operator(tenant_b, sub="op-b"),
                kind="vault-role",
                name="rdc-vault",
            )
        async with sessionmaker() as session:
            result_a = await create_or_get_node(
                session,
                _operator(tenant_a, sub="op-a"),
                kind="vault-role",
                name="rdc-vault",
            )

    assert result_a.was_created is True

    # Two independent rows — one per tenant.
    async with sessionmaker() as session:
        all_rows = (await session.execute(select(GraphNode))).scalars().all()
    by_tenant = {row.tenant_id: row for row in all_rows}
    assert set(by_tenant.keys()) == {tenant_a, tenant_b}


# ---------------------------------------------------------------------------
# Bootstrap → annotate end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_node_then_annotate_round_trip() -> None:
    """The issue's bootstrap acceptance: seed two nodes then annotate an edge.

    The end-to-end criterion (issue body: "a test that creates a node
    then annotates an edge between two freshly-created nodes"). Drives
    both verbs through the substrate to prove the empty-tenant
    bootstrap reaches a working topology state without the CLI refresh.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with (
        patch(_PUBLISH, new=AsyncMock()),
        patch("meho_backplane.topology.annotate.publish_event", new=AsyncMock()),
    ):
        async with sessionmaker() as session:
            first = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="principal",
                name="k8s-sa-prod",
            )
        async with sessionmaker() as session:
            second = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
            )
        async with sessionmaker() as session:
            edge = await annotate_edge(
                session,
                _operator(tenant_id),
                NodeRef("k8s-sa-prod", "principal"),
                "authenticates-via",
                NodeRef("rdc-vault", "vault-role"),
                note="bootstrap test",
            )

    assert first.was_created is True
    assert second.was_created is True
    assert edge.source == "curated"
    assert edge.kind == "authenticates-via"
    assert edge.from_node_id == first.node.id
    assert edge.to_node_id == second.node.id


# ---------------------------------------------------------------------------
# graph_node_history diff-on-write hook (G0.18-T6 #1359, RDC #789 F-A)
# ---------------------------------------------------------------------------
#
# Pre-#1359, ``create_or_get_node`` wrote audit_log + broadcast but no
# ``graph_node_history`` row, so a manual seed was invisible to
# ``query_topology kind=history|timeline`` even though it appeared in
# ``query_audit`` — an audit-vs-graph-history asymmetry that breaks the
# "when was this node added?" answer for any tenant that bootstrapped
# via manual seeds rather than the refresh service. The hook now mirrors
# :mod:`refresh` / :mod:`annotate`: one row per meaningful call sharing
# the call's pre-allocated ``audit_id``, with heartbeat-only re-seeds
# (``seeded_at`` / ``last_seen`` only) deliberately skipped to honour
# the "no double-write" criterion on the get path.


@pytest.mark.asyncio
async def test_create_node_emits_created_history_row_visible_to_history_verb() -> None:
    """Fresh insert emits a ``created`` history row, surfaced by ``query_history``.

    Anchor acceptance criterion: ``query_topology kind=history
    <manually-seeded-node>`` returns the create entry (issue body).
    Drives the substrate end-to-end — seed via :func:`create_or_get_node`
    then read back through :func:`query_history` — so a future drift in
    the snapshot column projection, the audit_id pre-allocation, or the
    history walk's index would fail this test rather than surfacing on
    the next dogfood cycle.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="seeded from INVENTORY.md L42",
                evidence_url="https://example.test/inv#L42",
            )

    # Exactly one history row landed for this node, ``created``.
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(GraphNodeHistory).where(
                        GraphNodeHistory.tenant_id == tenant_id,
                        GraphNodeHistory.node_id == result.node.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    history = rows[0]
    assert history.change_kind == GraphHistoryChangeKind.CREATED.value
    # ``snapshot.before`` is None on a fresh insert; ``after`` carries
    # the full post-insert projection so the temporal-replay verbs can
    # reconstruct the seed without joining back against live tables.
    assert history.snapshot["before"] is None
    after = history.snapshot["after"]
    assert after["kind"] == "vault-role"
    assert after["name"] == "rdc-vault"
    assert after["discovered_by"] == "op-1"
    assert after["properties"]["note"] == "seeded from INVENTORY.md L42"
    assert after["properties"]["evidence_url"] == "https://example.test/inv#L42"

    # The history row's ``audit_id`` references the create_node call's
    # ``audit_log`` row — same chassis "audit-id pre-allocation" pattern
    # refresh / annotate use so the query layer can join history back
    # against audit to recover the causing principal.
    async with sessionmaker() as session:
        audit = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
            .scalars()
            .one()
        )
    assert history.audit_id == audit.id

    # query_history surfaces the row through the operator-visible verb.
    result_history = await query_history(_operator(tenant_id), "rdc-vault", kind="vault-role")
    assert len(result_history.rows) == 1
    assert result_history.rows[0].change_kind == GraphHistoryChangeKind.CREATED.value
    assert result_history.rows[0].audit_id == audit.id


@pytest.mark.asyncio
async def test_create_node_seed_appears_in_timeline_verb() -> None:
    """``query_topology kind=timeline`` includes the manually-seeded node.

    Anchor acceptance criterion: ``kind=timeline`` includes the seed
    (issue body). The timeline UNIONs ``graph_node_history`` +
    ``graph_edge_history`` tenant-scoped — pre-#1359 the seed was
    audited but invisible to this verb because no history row existed.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            result = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="principal",
                name="k8s-sa-prod",
            )

    timeline = await query_timeline(_operator(tenant_id))
    # Exactly one entry — the create_node seed.
    assert len(timeline.rows) == 1
    entry = timeline.rows[0]
    assert entry.source == "node"
    assert entry.resource_id == result.node.id
    assert entry.change_kind == GraphHistoryChangeKind.CREATED.value


@pytest.mark.asyncio
async def test_create_node_idempotent_reseed_does_not_double_write_history() -> None:
    """No double-write when ``create_or_get_node`` hits the get path.

    Acceptance criterion: an idempotent re-seed with the same
    ``(kind, name)`` and identical ``(note, evidence_url)`` must not
    emit a phantom ``updated`` history row. The only changes between
    the two calls are the heartbeat fields (``seeded_at`` is
    ``datetime.now(UTC).isoformat()``; ``last_seen`` is refreshed),
    which mirror the heartbeat-skip discipline in
    :func:`refresh._update_existing_node` (``is_meaningful_update``)
    and :func:`annotate._annotate_curated_is_meaningful`. Without this
    guard, repeated MCP polls of a bootstrap seed would balloon the
    history table with empty UPDATED rows.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            first = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="same",
                evidence_url="https://example.test/same",
            )
        async with sessionmaker() as session:
            second = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="same",
                evidence_url="https://example.test/same",
            )

    assert first.was_created is True
    assert second.was_created is False

    # Only the first call's ``created`` row exists — the second's
    # heartbeat-only re-seed deliberately emits no history row.
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(GraphNodeHistory).where(
                        GraphNodeHistory.tenant_id == tenant_id,
                        GraphNodeHistory.node_id == first.node.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].change_kind == GraphHistoryChangeKind.CREATED.value


@pytest.mark.asyncio
async def test_create_node_meaningful_reseed_emits_updated_history_row() -> None:
    """A re-seed with a *different* note emits one ``updated`` row.

    Distinguishes the heartbeat-only no-op skip (covered above) from a
    real property change — the operator updated ``note`` /
    ``evidence_url`` on an already-seeded row and that mutation must
    land in the history table so the operator-visible "what changed
    for this node?" query reflects it.
    """
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            first = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="first",
            )
        async with sessionmaker() as session:
            second = await create_or_get_node(
                session,
                _operator(tenant_id),
                kind="vault-role",
                name="rdc-vault",
                note="second",
            )

    assert first.was_created is True
    assert second.was_created is False

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(GraphNodeHistory)
                    .where(
                        GraphNodeHistory.tenant_id == tenant_id,
                        GraphNodeHistory.node_id == first.node.id,
                    )
                    .order_by(GraphNodeHistory.history_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    kinds = [row.change_kind for row in rows]
    assert kinds == [GraphHistoryChangeKind.CREATED.value, GraphHistoryChangeKind.UPDATED.value]
    # ``snapshot.before`` on the updated row captures the pre-mutation
    # ``note='first'`` — the diff-on-write hook discipline that lets
    # ``meho topology diff`` reconstruct the operator's edit without
    # joining live tables.
    update_row = rows[1]
    assert update_row.snapshot["before"]["properties"]["note"] == "first"
    assert update_row.snapshot["after"]["properties"]["note"] == "second"


@pytest.mark.asyncio
async def test_create_node_history_does_not_leak_across_tenants() -> None:
    """A history row for tenant-B is invisible to a tenant-A history query.

    The history walk's first WHERE clause is ``tenant_id = :tenant_id``;
    this test pins that the create_node hook also wrote the row under
    the correct tenant scope so the tenant-isolation invariant the
    rest of the substrate enforces holds at the seed-emit point too.
    """
    tenant_a = await _seed_tenant(slug="tenant-a")
    tenant_b = await _seed_tenant(slug="tenant-b")
    sessionmaker = get_sessionmaker()

    with patch(_PUBLISH, new=AsyncMock()):
        async with sessionmaker() as session:
            await create_or_get_node(
                session,
                _operator(tenant_b, sub="op-b"),
                kind="vault-role",
                name="rdc-vault",
            )
        async with sessionmaker() as session:
            await create_or_get_node(
                session,
                _operator(tenant_a, sub="op-a"),
                kind="vault-role",
                name="rdc-vault",
            )

    timeline_a = await query_timeline(_operator(tenant_a, sub="op-a"))
    assert len(timeline_a.rows) == 1
    timeline_b = await query_timeline(_operator(tenant_b, sub="op-b"))
    assert len(timeline_b.rows) == 1
