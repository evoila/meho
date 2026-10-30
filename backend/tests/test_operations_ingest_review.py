# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G0.7-T4 review-queue state machine.

Coverage matrix (Task #402 acceptance criteria):

* ``parse_connector_id`` — every example from
  ``docs/architecture/connectors.md`` round-trips correctly.
* ``get_review_payload`` returns groups + per-group operation
  counts; payload structure round-trips against a seeded connector
  with 3 groups + 30 ops.
* ``edit_group`` updates ``when_to_use`` + ``name``; writes one
  audit row.
* ``edit_op`` updates per-op overrides; writes one audit row.
* ``edit_op`` ``is_enabled=False`` override sticks: subsequent
  ``enable_connector`` does NOT clobber the operator-set
  ``is_enabled=False``.
* ``enable_connector`` transitions groups + cascades to child
  ops; idempotent re-run writes no audit row.
* ``disable_connector`` transitions groups + cascades; idempotent.
* ``enable_group`` per-group flow works.
* State-machine guards: re-running on already-target state is a
  no-op (no audit row), not an error.
* Tenant boundary: tenant A's operator cannot transition tenant
  B's connector (raises :class:`ConnectorNotFoundError`).
* Built-in (``tenant_id IS NULL``) requires
  :class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`.
* Audit rows present for every transition + edit; idempotent
  no-ops do not produce rows.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest` which
pre-migrates the schema to head. Each test owns its own seed data
in a tmp-path DB file, so cross-test contamination is impossible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    OperationPolicyOverride,
)
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.ingest import (
    AmbiguousConnectorScopeError,
    BuiltinConnectorWriteForbiddenError,
    ConnectorNotFoundError,
    ConnectorReviewPayload,
    EditOpWarning,
    EndpointDescriptorProto,
    InvalidStateTransitionError,
    OpIdCollision,
    ReviewService,
    ensure_connector_class_registered,
    parse_connector_id,
    register_ingested_operations,
    run_llm_grouping,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction depends on transitively.

    Mirrors the same pattern :mod:`tests.test_db_endpoint_descriptor`
    uses: the autouse ``_default_database_url`` fixture pins
    ``DATABASE_URL``; Keycloak/Vault env vars come from each test
    file. ``get_settings.cache_clear()`` brackets keep a stale
    cached instance from leaking across tests.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_JWT = "header.payload.signature"


def _make_operator(
    *,
    tenant_id: uuid.UUID,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    sub: str | None = None,
    platform_admin: bool = False,
) -> Operator:
    """Build a frozen :class:`Operator` with default test fields."""
    return Operator(
        sub=sub or f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=role,
        platform_admin=platform_admin,
    )


async def _seed_connector(
    *,
    tenant_id: uuid.UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
    group_count: int = 3,
    ops_per_group: int = 10,
    review_status: str = "staged",
    op_is_enabled: bool = False,
) -> list[uuid.UUID]:
    """Insert *group_count* groups, each with *ops_per_group* operations.

    Returns the list of inserted group ids in deterministic order
    (``group-0``, ``group-1``, ...). Each group's child ops carry
    the deterministic ``op_id`` ``"GET:/api/v1/<group_key>/<i>"`` so
    tests can target an individual op precisely.
    """
    sessionmaker = get_sessionmaker()
    group_ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for g_index in range(group_count):
            group_id = uuid.uuid4()
            group_key = f"group-{g_index}"
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    group_key=group_key,
                    name=f"Group {g_index}",
                    when_to_use=f"Use group {g_index} for things.",
                    review_status=review_status,
                ),
            )
            group_ids.append(group_id)
            for o_index in range(ops_per_group):
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=product,
                        version=version,
                        impl_id=impl_id,
                        op_id=f"GET:/api/v1/{group_key}/{o_index}",
                        source_kind="ingested",
                        method="GET",
                        path=f"/api/v1/{group_key}/{o_index}",
                        group_id=group_id,
                        summary=f"Operation {o_index} in {group_key}",
                        is_enabled=op_is_enabled,
                    ),
                )
        await session.commit()
    return group_ids


async def _add_ungrouped_op(
    *,
    tenant_id: uuid.UUID | None,
    op_id: str,
    method: str | None,
    is_enabled: bool = False,
    source_kind: str = "ingested",
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> None:
    """Insert one descriptor with ``group_id IS NULL`` (the grouping pass left it out).

    The #3681 fixture shape: a descriptor the LLM grouping pass never
    bucketed. ``method=None`` seeds a typed / composite op (path NULL too);
    a string method seeds an ingested read/write.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                source_kind=source_kind,
                method=method,
                path=(f"/api/v1/{op_id}" if method is not None else None),
                group_id=None,
                summary=f"Ungrouped {op_id}",
                is_enabled=is_enabled,
            )
        )
        await session.commit()


async def _count_audit_rows(*, op_id: str | None = None) -> int:
    """Return the number of ``audit_log`` rows.

    When *op_id* is given, restrict to rows whose ``path`` matches.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(AuditLog)
        if op_id is not None:
            stmt = stmt.where(AuditLog.path == op_id)
        result = await session.execute(stmt)
        return len(list(result.scalars().all()))


async def _latest_audit_row(*, op_id: str) -> AuditLog:
    """Return the most-recent ``audit_log`` row with ``path == op_id``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(AuditLog).where(AuditLog.path == op_id).order_by(AuditLog.occurred_at.desc())
        result = await session.execute(stmt)
        row = result.scalars().first()
        assert row is not None, f"expected an audit row with path={op_id}"
        return row


async def _ops_enabled_state(
    *,
    tenant_id: uuid.UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> dict[str, bool]:
    """Return ``{op_id: is_enabled}`` for every op under the connector."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
        )
        if tenant_id is None:
            stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            stmt = stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return {op.op_id: op.is_enabled for op in result.scalars().all()}


async def _group_statuses(
    *,
    tenant_id: uuid.UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> dict[str, str]:
    """Return ``{group_key: review_status}`` for every group under the connector."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
        )
        if tenant_id is None:
            stmt = stmt.where(OperationGroup.tenant_id.is_(None))
        else:
            stmt = stmt.where(OperationGroup.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return {group.group_key: group.review_status for group in result.scalars().all()}


# ---------------------------------------------------------------------------
# parse_connector_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "connector_id,expected",
    [
        ("vmware-rest-9.0", ("vmware", "9.0", "vmware-rest")),
        ("nsx-4.2", ("nsx", "4.2", "nsx")),
        ("harbor-2.x", ("harbor", "2.x", "harbor")),
        ("hetzner-robot-2026-04", ("hetzner", "2026-04", "hetzner-robot")),
        ("vault-1.x", ("vault", "1.x", "vault")),
        ("k8s-1.x", ("k8s", "1.x", "k8s")),
        ("sddc-manager-9.0", ("sddc", "9.0", "sddc-manager")),
    ],
)
def test_parse_connector_id_round_trip(
    connector_id: str,
    expected: tuple[str, str, str],
) -> None:
    """Every connector_id in the architecture-doc inventory parses correctly."""
    assert parse_connector_id(connector_id) == expected


@pytest.mark.parametrize(
    "bad_connector_id",
    [
        "no-version",
        "missing-digit-suffix",
        "",
        "-9.0",
        "9.0",
    ],
)
def test_parse_connector_id_rejects_malformed_input(bad_connector_id: str) -> None:
    """Inputs that don't match the ``<impl_id>-<version>`` shape raise ``ValueError``."""
    with pytest.raises(ValueError):
        parse_connector_id(bad_connector_id)


# ---------------------------------------------------------------------------
# get_review_payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_review_payload_returns_groups_with_counts() -> None:
    """Seeded connector with 3 groups + 30 ops renders into the payload model."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=3,
        ops_per_group=10,
        review_status="staged",
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    assert isinstance(payload, ConnectorReviewPayload)
    assert payload.connector_id == "vmware-rest-9.0"
    assert payload.product == "vmware"
    assert payload.version == "9.0"
    assert payload.impl_id == "vmware-rest"
    assert payload.tenant_id == tenant_id
    assert payload.total_op_count == 30
    assert len(payload.groups) == 3
    group_keys = {group.group_key for group in payload.groups}
    assert group_keys == {"group-0", "group-1", "group-2"}
    for group in payload.groups:
        assert group.op_count == 10
        assert group.review_status == "staged"
        assert len(group.ops) == 10
        for op in group.ops:
            assert op.is_enabled is False
            assert op.safety_level == "safe"
            assert op.needs_reingest is False
    assert payload.needs_reingest_op_count == 0


@pytest.mark.asyncio
async def test_get_review_payload_surfaces_needs_reingest_flag_and_count() -> None:
    """#3102: the per-op flag renders, and the rollup counts ungrouped rows too.

    Migration 0076 flags descriptor rows whose stored ``parameter_schema``
    cannot self-resolve. The review payload is the operator's enumeration
    surface: each rendered op carries ``needs_reingest``, and
    ``needs_reingest_op_count`` counts the **full** descriptor universe
    (like ``total_op_count + ungrouped_op_count``) so a flagged op whose
    ``group_id`` is null still shows up in the rollup.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=2)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        flagged = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "GET:/api/v1/group-0/0",
                    EndpointDescriptor.tenant_id == tenant_id,
                )
            )
        ).scalar_one()
        flagged.needs_reingest = True
        # A flagged row outside any group -- invisible to the rendered
        # groups, but the rollup must still count it.
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id="POST:/api/v1/ungrouped/upload",
                source_kind="ingested",
                method="POST",
                path="/api/v1/ungrouped/upload",
                group_id=None,
                summary="Ungrouped flagged op",
                is_enabled=True,
                needs_reingest=True,
            )
        )
        await session.commit()
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    ops_by_id = {op.op_id: op for group in payload.groups for op in group.ops}
    assert ops_by_id["GET:/api/v1/group-0/0"].needs_reingest is True
    assert ops_by_id["GET:/api/v1/group-0/1"].needs_reingest is False
    assert payload.needs_reingest_op_count == 2
    assert payload.ungrouped_op_count == 1


async def _add_stored_write(
    *,
    tenant_id: uuid.UUID,
    op_id: str,
    safety_level: str,
    requires_approval: bool,
    source_kind: str = "ingested",
    is_enabled: bool = True,
) -> None:
    """Insert one stored write row the way an earlier ingest left it."""
    method, path = op_id.split(":", 1)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id=op_id,
                source_kind=source_kind,
                method=method,
                path=path,
                group_id=None,
                summary=f"Stored {op_id}",
                safety_level=safety_level,
                requires_approval=requires_approval,
                is_enabled=is_enabled,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_review_payload_reports_stored_deletes_below_destructive() -> None:
    """#3827: stored deletes that an earlier ingest left too weak are listed with a fix.

    Rows persisted before the parser and floor changes keep their old
    posture until the next ingest. The review report shows every stored
    ingested delete-shaped row below ``destructive`` next to what a fresh
    ingest would assign, and flags the rows that are weaker than that.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    # The issue's shape: a category delete stored at caution, no approval.
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="DELETE:/api/cis/tagging/category/{category_id}",
        safety_level="caution",
        requires_approval=False,
    )
    # A tag detach stored at caution: a POST the floor now treats as a removal.
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="POST:/api/cis/tagging/tag-association?action=detach",
        safety_level="caution",
        requires_approval=False,
    )
    # A generic delete stored dangerous without approval (pre-#3827 parser).
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="DELETE:/vcenter/datastore/{datastore}",
        safety_level="dangerous",
        requires_approval=False,
    )
    # A generic delete already at the current parser posture.
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="DELETE:/vcenter/folder/{folder}",
        safety_level="dangerous",
        requires_approval=True,
    )
    # Not delete-shaped: an ordinary write, and a typed delete, stay off the report.
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="POST:/vcenter/folder",
        safety_level="caution",
        requires_approval=False,
    )
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="DELETE:/typed/thing/{id}",
        safety_level="dangerous",
        requires_approval=False,
        source_kind="typed",
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    rows = {row.op_id: row for row in payload.delete_ops_below_destructive}
    assert sorted(rows) == [
        "DELETE:/api/cis/tagging/category/{category_id}",
        "DELETE:/vcenter/datastore/{datastore}",
        "DELETE:/vcenter/folder/{folder}",
        "POST:/api/cis/tagging/tag-association?action=detach",
    ]
    assert payload.delete_ops_needing_correction_count == 3

    category = rows["DELETE:/api/cis/tagging/category/{category_id}"]
    assert category.safety_level == "caution"
    assert category.requires_approval is False
    assert category.expected_safety_level == "destructive"
    assert category.expected_requires_approval is True
    assert category.needs_correction is True
    assert "a fresh ingest gives destructive" in category.reason

    detach = rows["POST:/api/cis/tagging/tag-association?action=detach"]
    assert detach.method == "POST"
    assert detach.expected_safety_level == "destructive"
    assert detach.needs_correction is True

    datastore = rows["DELETE:/vcenter/datastore/{datastore}"]
    assert datastore.expected_safety_level == "dangerous"
    assert datastore.expected_requires_approval is True
    assert datastore.needs_correction is True
    assert "runs at once for a human operator" in datastore.reason

    folder = rows["DELETE:/vcenter/folder/{folder}"]
    assert folder.needs_correction is False
    assert "waits for approval at dangerous" in folder.reason


@pytest.mark.asyncio
async def test_review_payload_reports_stored_vendor_delete_action_names() -> None:
    """#4632: a stored VI-JSON delete that an earlier ingest left too weak is listed.

    ``POST:/VirtualMachine/{moId}/Destroy_Task`` is a POST, so neither its
    method nor a floor marks it. Its action name does. A lookalike stays
    off the report.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="POST:/VirtualMachine/{moId}/Destroy_Task",
        safety_level="caution",
        requires_approval=False,
    )
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="POST:/VirtualMachine/{moId}/Reset_Task",
        safety_level="caution",
        requires_approval=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    rows = {row.op_id: row for row in payload.delete_ops_below_destructive}
    assert sorted(rows) == ["POST:/VirtualMachine/{moId}/Destroy_Task"]
    assert payload.delete_ops_needing_correction_count == 1
    destroy = rows["POST:/VirtualMachine/{moId}/Destroy_Task"]
    assert destroy.method == "POST"
    assert destroy.expected_safety_level == "dangerous"
    assert destroy.expected_requires_approval is True
    assert destroy.needs_correction is True
    assert "a fresh ingest gives dangerous" in destroy.reason


@pytest.mark.asyncio
async def test_review_payload_delete_report_is_empty_without_stored_deletes() -> None:
    """A connector with only reads and ordinary writes reports no deletes."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=2)
    await _add_stored_write(
        tenant_id=tenant_id,
        op_id="POST:/vcenter/folder",
        safety_level="caution",
        requires_approval=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    assert payload.delete_ops_below_destructive == []
    assert payload.delete_ops_needing_correction_count == 0


@pytest.mark.asyncio
async def test_review_payload_reports_ungrouped_descriptor_count() -> None:
    """#3681: the review payload reports ungrouped descriptors so an operator can see the gap.

    Ungrouped descriptors (``group_id IS NULL``) never appear in any
    rendered group's op list, so the reconciling ``ungrouped_op_count`` is
    the operator's read-only signal that they exist. It counts ungrouped
    reads and writes alike, whatever their ``is_enabled`` state.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=2)
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/ungrouped/list",
        method="GET",
        is_enabled=True,
    )
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="POST:/api/v1/ungrouped/create",
        method="POST",
        is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    # The two grouped ops are the rendered total; the two ungrouped ops
    # (one read, one write) are the reconciling remainder and appear in no
    # rendered group.
    assert payload.total_op_count == 2
    assert payload.ungrouped_op_count == 2
    grouped_op_ids = {op.op_id for group in payload.groups for op in group.ops}
    assert "POST:/api/v1/ungrouped/create" not in grouped_op_ids
    assert "GET:/api/v1/ungrouped/list" not in grouped_op_ids


@pytest.mark.asyncio
async def test_review_payload_lists_ungrouped_ops_with_state() -> None:
    """#3870: the review lists every ungrouped op with its enabled state and safety level.

    An enabled ungrouped op is served by dispatch like any grouped one, so
    the review must show it. ``groups`` plus ``ungrouped_ops`` is the full
    op set, and the list length matches ``ungrouped_op_count``.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=2)
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="POST:/api/v1/ungrouped/create",
        method="POST",
        is_enabled=False,
    )
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/ungrouped/list",
        method="GET",
        is_enabled=True,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    assert [(op.op_id, op.is_enabled) for op in payload.ungrouped_ops] == [
        ("GET:/api/v1/ungrouped/list", True),
        ("POST:/api/v1/ungrouped/create", False),
    ]
    assert len(payload.ungrouped_ops) == payload.ungrouped_op_count
    assert all(op.safety_level for op in payload.ungrouped_ops)


@pytest.mark.asyncio
async def test_review_payload_for_tenant_connector_without_groups() -> None:
    """#3870: a tenant connector with ops but no group rows still has a review.

    The grouping pass can fail and leave descriptor rows with no group.
    Dispatch still serves the enabled ones, so the review must resolve
    the connector from its op rows, return ``groups=[]``, and list every
    op in ``ungrouped_ops``.
    """
    tenant_id = uuid.uuid4()
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/ungrouped/list",
        method="GET",
        is_enabled=True,
    )
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="POST:/api/v1/ungrouped/create",
        method="POST",
        is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)

    assert payload.tenant_id == tenant_id
    assert payload.groups == []
    assert payload.total_op_count == 0
    assert payload.ungrouped_op_count == 2
    assert [(op.op_id, op.is_enabled) for op in payload.ungrouped_ops] == [
        ("GET:/api/v1/ungrouped/list", True),
        ("POST:/api/v1/ungrouped/create", False),
    ]


@pytest.mark.asyncio
async def test_review_payload_for_builtin_connector_without_groups() -> None:
    """#3870: a built-in connector with ops but no group rows has a review too.

    Both built-in read paths must find it: the operator's own-tenant
    probe falls back to the built-in row, and the admin path's explicit
    ``tenant_id=None`` probe resolves it directly.
    """
    operator_tenant = uuid.uuid4()
    await _add_ungrouped_op(
        tenant_id=None,
        op_id="GET:/api/v1/ungrouped/list",
        method="GET",
        is_enabled=True,
    )

    fallback = ReviewService(_make_operator(tenant_id=operator_tenant, role=TenantRole.OPERATOR))
    payload = await fallback.get_review_payload("vmware-rest-9.0", operator_tenant)
    assert payload.tenant_id is None
    assert payload.groups == []
    assert [op.op_id for op in payload.ungrouped_ops] == ["GET:/api/v1/ungrouped/list"]
    assert payload.ungrouped_op_count == 1

    admin = ReviewService(_make_operator(tenant_id=operator_tenant, platform_admin=True))
    payload = await admin.get_review_payload("vmware-rest-9.0", None)
    assert payload.tenant_id is None
    assert payload.groups == []
    assert [op.op_id for op in payload.ungrouped_ops] == ["GET:/api/v1/ungrouped/list"]


@pytest.mark.asyncio
async def test_review_payload_tenant_ops_without_groups_shadow_builtin_groups() -> None:
    """#3870: a tenant row made only of op rows still counts as the tenant's row.

    Dispatch treats the tenant's descriptor rows as authoritative, so the
    review resolver must see them too. With a built-in row present as
    well, the un-preferred probe reports the ambiguity instead of
    silently reviewing the built-in row.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=None, group_count=1, ops_per_group=1)
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/tenant-only",
        method="GET",
        is_enabled=True,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    with pytest.raises(AmbiguousConnectorScopeError):
        await service.get_review_payload("vmware-rest-9.0", tenant_id)

    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id, prefer="tenant")
    assert payload.tenant_id == tenant_id
    assert payload.groups == []
    assert [op.op_id for op in payload.ungrouped_ops] == ["GET:/api/v1/tenant-only"]


@pytest.mark.asyncio
async def test_review_counts_do_not_bleed_across_duplicate_scope_registrations() -> None:
    """A duplicate ``connector_id`` under two scopes keeps counts isolated (#2977).

    The catalog can legitimately hold the same ``connector_id``
    (``vmware-rest-9.0``) once per scope — the
    ``(product, version, impl_id, tenant_id)`` partial-unique split makes
    builtin+tenant and cross-tenant twins legal by design. The review's
    ``load_groups`` / ``load_ops_in_groups`` / ``count_ops_in_scope``
    helpers all key on that same scoped triple, so one scope's review
    counts only its own rows and never bleeds a twin's operations in.

    (The #2977 report read ``ungrouped_op_count > total_op_count`` as
    cross-duplicate bleed; that is documented semantics — ``total``
    counts only rendered-group ops, and ``total + ungrouped`` reconciles
    to the listing's ``operation_count`` — so this test pins the real
    scope-isolation invariant the symptom was mistaken for.)
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    # The same connector_id under two tenants, with different op counts.
    await _seed_connector(tenant_id=tenant_a, group_count=2, ops_per_group=3)  # 6 ops
    await _seed_connector(tenant_id=tenant_b, group_count=1, ops_per_group=4)  # 4 ops

    payload_a = await ReviewService(_make_operator(tenant_id=tenant_a)).get_review_payload(
        "vmware-rest-9.0",
        tenant_a,
    )
    payload_b = await ReviewService(_make_operator(tenant_id=tenant_b)).get_review_payload(
        "vmware-rest-9.0",
        tenant_b,
    )

    # Each scope counts only its own operations — never the 10-op sum a
    # bleed across the two registrations would produce.
    assert payload_a.tenant_id == tenant_a
    assert payload_a.total_op_count == 6
    assert payload_a.ungrouped_op_count == 0
    assert payload_b.tenant_id == tenant_b
    assert payload_b.total_op_count == 4
    assert payload_b.ungrouped_op_count == 0


@pytest.mark.asyncio
async def test_get_review_payload_raises_when_no_rows() -> None:
    """A connector triple with no rows yields :class:`ConnectorNotFoundError`."""
    tenant_id = uuid.uuid4()
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload("vmware-rest-9.0", tenant_id)


@pytest.mark.asyncio
async def test_get_review_payload_no_audit_row_written() -> None:
    """Read path doesn't write a service-level audit row (HTTP layer audits the request)."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    await service.get_review_payload("vmware-rest-9.0", tenant_id)
    assert await _count_audit_rows() == 0


@pytest.mark.asyncio
async def test_get_review_payload_falls_back_to_builtin_for_operator_tenant() -> None:
    """G0.13-T5 (#1135): operator's-tenant probe falling through to built-in.

    The listing endpoint already returns built-in (``tenant_id IS
    NULL``) connectors to every operator. The review endpoint's
    docstring promises the same scope but the route handler calls
    ``get_review_payload(connector_id, operator.tenant_id)`` — a
    single-pass lookup with ``WHERE tenant_id = X`` misses every
    global row. The fix here makes the service do a two-pass lookup:
    own-tenant first, then ``tenant_id IS NULL``. The non-admin
    operator role is used deliberately — the bug only surfaced for
    that role (admins could pass ``tenant_id=None`` directly).
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,  # built-in / global connector
        group_count=2,
        ops_per_group=3,
        review_status="staged",
    )
    operator = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.OPERATOR,
    )
    service = ReviewService(operator)

    payload = await service.get_review_payload("vmware-rest-9.0", operator_tenant)

    assert payload.connector_id == "vmware-rest-9.0"
    assert payload.tenant_id is None  # rendered scope reflects the fallback
    assert payload.total_op_count == 6
    assert len(payload.groups) == 2


@pytest.mark.asyncio
async def test_get_review_payload_does_not_fall_back_for_cross_tenant_probe() -> None:
    """G0.13-T5 (#1135): cross-tenant probes stay 404, not 200.

    The fallback only triggers when the caller passes the operator's
    own tenant_id. A probe with a *different* tenant_id (operator A
    asking after tenant B's connector) must keep the existing
    cross-tenant 404 conflation — otherwise the fix would open a
    cross-tenant info-leak surface.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    # Built-in connector exists; tenant B has nothing.
    await _seed_connector(
        tenant_id=None,
        group_count=1,
        ops_per_group=1,
        review_status="staged",
    )
    operator = _make_operator(
        tenant_id=tenant_a,
        role=TenantRole.OPERATOR,
    )
    service = ReviewService(operator)

    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload("vmware-rest-9.0", tenant_b)


@pytest.mark.asyncio
async def test_get_review_payload_non_existent_connector_still_404() -> None:
    """G0.13-T5 (#1135): genuinely missing connector_id still raises after both passes.

    The two-pass fallback must not mask the "connector doesn't exist
    anywhere" case: neither the operator's-tenant probe nor the
    built-in probe finds a row, so the exception still propagates.
    """
    operator_tenant = uuid.uuid4()
    operator = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.OPERATOR,
    )
    service = ReviewService(operator)

    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload("vmware-rest-9.0", operator_tenant)


@pytest.mark.asyncio
async def test_get_review_payload_ambiguous_tenant_and_builtin_raises() -> None:
    """G0.26-T1 (#1801): a label mapping to a tenant row AND a built-in row is ambiguous.

    Supersedes the prior #1135 "first-pass wins" guard. When an
    operator's tenant has a curated row at ``(product, version,
    impl_id)`` *and* a built-in row exists at the same triple, neither
    the read (``/review``) nor the write (``/enable-reads``) path may
    silently pick one — they raise :class:`AmbiguousConnectorScopeError`
    so the operator disambiguates. The candidate list enumerates both
    row-scopes (tenant + built-in).
    """
    operator_tenant = uuid.uuid4()
    # Tenant-curated row (3 ops total).
    await _seed_connector(
        tenant_id=operator_tenant,
        group_count=1,
        ops_per_group=3,
        review_status="staged",
    )
    # Built-in row at the same triple (5 ops total — distinct count
    # so the silent-pick failure mode would be visible if it regressed).
    await _seed_connector(
        tenant_id=None,
        group_count=1,
        ops_per_group=5,
        review_status="staged",
    )
    operator = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.OPERATOR,
    )
    service = ReviewService(operator)

    with pytest.raises(AmbiguousConnectorScopeError) as excinfo:
        await service.get_review_payload("vmware-rest-9.0", operator_tenant)

    exc = excinfo.value
    assert exc.connector_id == "vmware-rest-9.0"
    # Two candidates: the built-in (tenant_id=None) and the operator's
    # tenant row. Sorted built-in-first by the exception.
    candidate_tenants = [c.tenant_id for c in exc.candidates]
    assert candidate_tenants == [None, operator_tenant]
    for candidate in exc.candidates:
        assert candidate.product == "vmware"
        assert candidate.version == "9.0"
        assert candidate.impl_id == "vmware-rest"


@pytest.mark.asyncio
async def test_ambiguous_scope_message_names_prefer_retry_values() -> None:
    """#2029: the ambiguous-scope message literally names the prefer retry.

    The structured 409 / ``-32602`` envelope is the operator's only
    actionable hint; per the error-message-shape contract it must name
    the remediation. #2029 turns the candidate envelope into a retry
    instruction, so the rendered message names both ``prefer=tenant``
    and ``prefer=builtin``.
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(tenant_id=operator_tenant, group_count=1, ops_per_group=3)
    await _seed_connector(tenant_id=None, group_count=1, ops_per_group=5)
    service = ReviewService(_make_operator(tenant_id=operator_tenant, role=TenantRole.OPERATOR))

    with pytest.raises(AmbiguousConnectorScopeError) as excinfo:
        await service.get_review_payload("vmware-rest-9.0", operator_tenant)

    message = str(excinfo.value)
    assert "prefer=tenant" in message
    assert "prefer=builtin" in message


@pytest.mark.asyncio
async def test_get_review_payload_prefer_tenant_returns_tenant_row() -> None:
    """#2029: ``prefer='tenant'`` resolves the tenant row, skipping the 409.

    Both a tenant-curated row (3 ops) and a built-in row (5 ops) exist;
    the distinct op counts prove which scope rendered. ``prefer='tenant'``
    returns the tenant row's payload instead of raising.
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(tenant_id=operator_tenant, group_count=1, ops_per_group=3)
    await _seed_connector(tenant_id=None, group_count=1, ops_per_group=5)
    service = ReviewService(_make_operator(tenant_id=operator_tenant, role=TenantRole.OPERATOR))

    payload = await service.get_review_payload(
        "vmware-rest-9.0",
        operator_tenant,
        prefer="tenant",
    )

    assert payload.tenant_id == operator_tenant
    assert payload.total_op_count == 3


@pytest.mark.asyncio
async def test_get_review_payload_prefer_builtin_returns_builtin_row() -> None:
    """#2029: ``prefer='builtin'`` resolves the built-in row, skipping the 409."""
    operator_tenant = uuid.uuid4()
    await _seed_connector(tenant_id=operator_tenant, group_count=1, ops_per_group=3)
    await _seed_connector(tenant_id=None, group_count=1, ops_per_group=5)
    service = ReviewService(_make_operator(tenant_id=operator_tenant, role=TenantRole.OPERATOR))

    payload = await service.get_review_payload(
        "vmware-rest-9.0",
        operator_tenant,
        prefer="builtin",
    )

    assert payload.tenant_id is None
    assert payload.total_op_count == 5


@pytest.mark.asyncio
async def test_get_review_payload_prefer_tenant_missing_row_raises_not_found() -> None:
    """#2029: ``prefer='tenant'`` against a built-in-only label is a genuine miss.

    The selector is exact, not a fall-back: an operator who explicitly
    asked for their tenant row gets :class:`ConnectorNotFoundError` (not
    a silent slide to the built-in row) when only the built-in exists.
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(tenant_id=None, group_count=1, ops_per_group=5)
    service = ReviewService(_make_operator(tenant_id=operator_tenant, role=TenantRole.OPERATOR))

    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload(
            "vmware-rest-9.0",
            operator_tenant,
            prefer="tenant",
        )


# ---------------------------------------------------------------------------
# edit_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_group_updates_when_to_use_and_name_and_writes_audit() -> None:
    """``edit_group`` mutates both fields and emits one audit row."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.edit_group(
        "vmware-rest-9.0",
        "group-0",
        tenant_id=tenant_id,
        when_to_use="Updated when-to-use.",
        name="Updated Name",
    )

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert "group-0" in statuses
    # Verify the updated fields landed.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.tenant_id == tenant_id,
            OperationGroup.group_key == "group-0",
        )
        result = await session.execute(stmt)
        group = result.scalar_one()
        assert group.when_to_use == "Updated when-to-use."
        assert group.name == "Updated Name"

    assert await _count_audit_rows(op_id="meho_connector_edit_group") == 1
    row = await _latest_audit_row(op_id="meho_connector_edit_group")
    assert row.method == "SERVICE"
    assert row.tenant_id == tenant_id
    payload: Any = row.payload
    assert payload["group_key"] == "group-0"
    assert sorted(payload["fields_updated"]) == ["name", "when_to_use"]


@pytest.mark.asyncio
async def test_edit_group_rejects_empty_edit() -> None:
    """Calling ``edit_group`` with no fields raises :class:`ValueError`."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    with pytest.raises(ValueError):
        await service.edit_group(
            "vmware-rest-9.0",
            "group-0",
            tenant_id=tenant_id,
        )


# ---------------------------------------------------------------------------
# edit_op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_op_updates_overrides_and_writes_audit() -> None:
    """``edit_op`` writes per-op overrides + emits one audit row."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=2)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    target_op = "GET:/api/v1/group-0/0"
    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        custom_description="A safer summary.",
        safety_level="dangerous",
        requires_approval=True,
        is_enabled=False,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.op_id == target_op,
        )
        result = await session.execute(stmt)
        op_row = result.scalar_one()
        assert op_row.custom_description == "A safer summary."
        # #4711: risk level and approval are an override; the row keeps
        # the classification.
        assert op_row.safety_level == "safe"
        assert op_row.requires_approval is False
        assert op_row.is_enabled is False
        override = (await session.execute(select(OperationPolicyOverride))).scalar_one()
        assert (override.tenant_id, override.op_id) == (tenant_id, target_op)
        assert (override.safety_level, override.requires_approval) == ("dangerous", True)

    assert await _count_audit_rows(op_id="meho_connector_edit_op") == 1
    row = await _latest_audit_row(op_id="meho_connector_edit_op")
    payload: Any = row.payload
    assert payload["op_id"] == target_op
    assert sorted(payload["fields_updated"]) == sorted(
        ["custom_description", "safety_level", "requires_approval", "is_enabled"],
    )
    assert payload["is_enabled_set_to"] is False
    assert payload["changes"] == {
        "safety_level": {"old": "safe", "new": "dangerous"},
        "requires_approval": {"old": False, "new": True},
        "is_enabled": {"old": False, "new": False},
    }
    assert payload["scope"] == "tenant"


@pytest.mark.asyncio
async def test_edit_op_rejects_invalid_safety_level() -> None:
    """Out-of-enum ``safety_level`` raises :class:`ValueError`."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    with pytest.raises(ValueError):
        await service.edit_op(
            "vmware-rest-9.0",
            "GET:/api/v1/group-0/0",
            tenant_id=tenant_id,
            safety_level="nuclear",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_edit_op_sets_llm_instructions_and_writes_audit_without_echo() -> None:
    """``edit_op(llm_instructions=...)`` persists the blob; audit names the field only."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    target_op = "GET:/api/v1/group-0/0"
    blob = {
        "when_to_call": "Inspect a single group's first op.",
        "output_shape": "object with id + status fields",
    }
    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        llm_instructions=blob,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.op_id == target_op,
        )
        op_row = (await session.execute(stmt)).scalar_one()
        assert op_row.llm_instructions == blob

    row = await _latest_audit_row(op_id="meho_connector_edit_op")
    payload: Any = row.payload
    assert payload["fields_updated"] == ["llm_instructions"]
    # The blob itself is NOT in the audit payload — operator-authored
    # prose belongs out of the audit table, same posture edit_group
    # takes for when_to_use.
    assert "llm_instructions" not in payload


@pytest.mark.asyncio
async def test_edit_op_requires_at_least_one_field() -> None:
    """Calling ``edit_op`` with every override set to ``None`` raises ``ValueError``."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    with pytest.raises(ValueError, match="llm_instructions"):
        await service.edit_op(
            "vmware-rest-9.0",
            "GET:/api/v1/group-0/0",
            tenant_id=tenant_id,
        )


@pytest.mark.asyncio
async def test_edit_op_explicit_null_custom_description_clears_column() -> None:
    """Explicit ``custom_description=None`` clears the column to SQL NULL (#2488).

    An operator who set an override and later wants it gone can pass an
    explicit ``null`` (the MCP tool's ``[string, null]`` inputSchema). The
    column must land NULL — not the empty string the pre-#2488
    empty-string workaround left — and the clear must be recorded in the
    audit payload's ``fields_updated`` like any other edit.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    target_op = "GET:/api/v1/group-0/0"

    # First set an override so the clear has something to remove.
    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        custom_description="An operator override to be cleared.",
    )
    # Now clear it with an explicit null.
    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        custom_description=None,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.op_id == target_op,
        )
        op_row = (await session.execute(stmt)).scalar_one()
        assert op_row.custom_description is None
        assert op_row.custom_description != ""

    row = await _latest_audit_row(op_id="meho_connector_edit_op")
    payload: Any = row.payload
    assert payload["fields_updated"] == ["custom_description"]


@pytest.mark.asyncio
async def test_edit_op_null_custom_description_with_other_field_clears_and_applies() -> None:
    """``custom_description=None`` alongside another field clears AND applies (#2488).

    The pre-#2488 bug silently dropped an explicit null when it rode
    alongside another override (the all-``None`` guard never fired, and
    the apply step skipped every ``None``). Both effects must now land:
    the description clears to NULL and the sibling field is written, with
    both names in ``fields_updated``.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    target_op = "GET:/api/v1/group-0/0"

    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        custom_description="An override that should be cleared below.",
    )
    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        custom_description=None,
        is_enabled=False,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.op_id == target_op,
        )
        op_row = (await session.execute(stmt)).scalar_one()
        assert op_row.custom_description is None
        assert op_row.is_enabled is False

    row = await _latest_audit_row(op_id="meho_connector_edit_op")
    payload: Any = row.payload
    assert sorted(payload["fields_updated"]) == ["custom_description", "is_enabled"]
    assert payload["is_enabled_set_to"] is False


@pytest.mark.asyncio
async def test_edit_op_omitted_custom_description_left_unchanged() -> None:
    """Omitting ``custom_description`` leaves the column untouched (#2488 regression).

    The sentinel default must keep the pre-#2488 "omitted = leave alone"
    semantics REST and the MCP omitted-key path both rely on: an edit that
    touches only ``is_enabled`` must not clobber an existing description.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    target_op = "GET:/api/v1/group-0/0"

    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        custom_description="Keep me across an unrelated edit.",
    )
    await service.edit_op(
        "vmware-rest-9.0",
        target_op,
        tenant_id=tenant_id,
        is_enabled=True,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.op_id == target_op,
        )
        op_row = (await session.execute(stmt)).scalar_one()
        assert op_row.custom_description == "Keep me across an unrelated edit."

    row = await _latest_audit_row(op_id="meho_connector_edit_op")
    payload: Any = row.payload
    assert payload["fields_updated"] == ["is_enabled"]


@pytest.mark.asyncio
async def test_edit_op_is_enabled_false_override_sticks_after_enable_connector() -> None:
    """Operator-set ``is_enabled=False`` survives a subsequent ``enable_connector``."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=3,
        review_status="staged",
        op_is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    # Operator marks op 0 as is_enabled=False explicitly.
    overridden_op = "GET:/api/v1/group-0/0"
    await service.edit_op(
        "vmware-rest-9.0",
        overridden_op,
        tenant_id=tenant_id,
        is_enabled=False,
    )

    # Now enable the whole connector. The cascade must respect the
    # operator override.
    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert enabled_state[overridden_op] is False, (
        "operator-set is_enabled=False clobbered by enable_connector cascade"
    )
    # The two non-overridden ops did flip to True.
    assert enabled_state["GET:/api/v1/group-0/1"] is True
    assert enabled_state["GET:/api/v1/group-0/2"] is True


# ---------------------------------------------------------------------------
# edit_op enable-time auto-shim advisory (G0.23-T4 #1630)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_op_enable_on_auto_shim_connector_returns_warning() -> None:
    """``is_enabled=True`` on a shim-backed op returns the advisory; the write still lands.

    The connector triple is registered to the synthesised
    ``GenericRestConnector`` auto-shim (the spec-ingest first-contact
    state), so dispatch is a guaranteed ``connector_unsupported`` /
    ``cause='unreplaced_auto_shim'`` dead end — the enable must say so
    while still applying the flag and writing the audit row.
    """
    tenant_id = uuid.uuid4()
    assert ensure_connector_class_registered(
        product="acme",
        version="1.2",
        impl_id="acme-rest",
        base_url=None,
    ), "expected a fresh auto-shim registration for the acme triple"
    await _seed_connector(
        tenant_id=tenant_id,
        product="acme",
        version="1.2",
        impl_id="acme-rest",
        group_count=1,
        ops_per_group=1,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    warnings = await service.edit_op(
        "acme-rest-1.2",
        "GET:/api/v1/group-0/0",
        tenant_id=tenant_id,
        is_enabled=True,
    )

    assert len(warnings) == 1
    warning = warnings[0]
    assert isinstance(warning, EditOpWarning)
    assert warning.code == "unreplaced_auto_shim"
    assert warning.connector_class == "AutoShim_acme_1_2_acme_rest"
    # The message names the missing-subclass requirement, the concrete
    # triple, the dispatch-time error it forecasts, and the doc ref —
    # the same remediation story result_connector_unsupported tells.
    assert "per-product Connector subclass" in warning.message
    assert "'acme'" in warning.message
    assert "'acme-rest'" in warning.message
    assert "connector_unsupported" in warning.message
    assert "re-ingesting the spec will NOT replace the shim" in warning.message
    assert "docs/codebase/spec-ingestion.md" in warning.message

    # Advisory, not a gate: the flag is set and the audit row written.
    enabled_state = await _ops_enabled_state(
        tenant_id=tenant_id,
        product="acme",
        version="1.2",
        impl_id="acme-rest",
    )
    assert enabled_state["GET:/api/v1/group-0/0"] is True
    assert await _count_audit_rows(op_id="meho_connector_edit_op") == 1


@pytest.mark.asyncio
async def test_edit_op_enable_on_hand_rolled_connector_returns_no_warning() -> None:
    """``is_enabled=True`` on a hand-rolled connector's op is unchanged — no advisory.

    ``vmware-rest-9.0`` resolves to ``VmwareRestConnector`` (priority
    1, hand-rolled — registered by the session-scoped connector-module
    import in ``conftest.py``), so the auto-shim probe stays silent.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    warnings = await service.edit_op(
        "vmware-rest-9.0",
        "GET:/api/v1/group-0/0",
        tenant_id=tenant_id,
        is_enabled=True,
    )

    assert warnings == []
    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert enabled_state["GET:/api/v1/group-0/0"] is True


@pytest.mark.asyncio
async def test_edit_op_non_enable_edits_skip_auto_shim_probe() -> None:
    """Only ``is_enabled=True`` triggers the probe — disable and field edits stay silent.

    Disabling a shim-backed op (or editing its safety level) is not a
    dispatch dead end, so warning there would train operators to
    ignore the advisory.
    """
    tenant_id = uuid.uuid4()
    assert ensure_connector_class_registered(
        product="acme",
        version="1.2",
        impl_id="acme-rest",
        base_url=None,
    )
    await _seed_connector(
        tenant_id=tenant_id,
        product="acme",
        version="1.2",
        impl_id="acme-rest",
        group_count=1,
        ops_per_group=1,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    assert (
        await service.edit_op(
            "acme-rest-1.2",
            "GET:/api/v1/group-0/0",
            tenant_id=tenant_id,
            is_enabled=False,
        )
        == []
    )
    assert (
        await service.edit_op(
            "acme-rest-1.2",
            "GET:/api/v1/group-0/0",
            tenant_id=tenant_id,
            safety_level="dangerous",
        )
        == []
    )


# ---------------------------------------------------------------------------
# enable_connector + disable_connector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_connector_transitions_and_cascades() -> None:
    """``enable_connector`` flips every group + every child op + writes one audit row."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=2,
        ops_per_group=3,
        review_status="staged",
        op_is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert set(statuses.values()) == {"enabled"}
    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert all(enabled_state.values()), "every child op should be is_enabled=True"

    assert await _count_audit_rows(op_id="meho_connector_enable") == 1
    row = await _latest_audit_row(op_id="meho_connector_enable")
    payload: Any = row.payload
    assert payload["to_status"] == "enabled"
    assert sorted(payload["transitioned_group_keys"]) == ["group-0", "group-1"]
    assert payload["ops_cascade_count"] == 6


@pytest.mark.asyncio
async def test_enable_connector_idempotent_writes_no_second_audit_row() -> None:
    """Re-running on a fully-enabled connector writes no second audit row."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=2,
        ops_per_group=2,
        review_status="staged",
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho_connector_enable") == 1

    # Second call — no-op.
    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho_connector_enable") == 1


@pytest.mark.asyncio
async def test_disable_connector_transitions_and_cascades() -> None:
    """``disable_connector`` flips groups + ops, even from a mixed source state."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=2,
        ops_per_group=2,
        review_status="enabled",
        op_is_enabled=True,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.disable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert set(statuses.values()) == {"disabled"}
    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert not any(enabled_state.values()), "every child op should be is_enabled=False"

    assert await _count_audit_rows(op_id="meho_connector_disable") == 1


@pytest.mark.asyncio
async def test_disable_connector_idempotent_writes_no_second_audit_row() -> None:
    """``disable_connector`` re-run on disabled groups is a no-op."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=1,
        review_status="disabled",
        op_is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.disable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho_connector_disable") == 0


@pytest.mark.asyncio
async def test_disable_then_re_enable_round_trip() -> None:
    """Re-enabling after disable restores ``is_enabled=True`` on every op."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=3,
        review_status="enabled",
        op_is_enabled=True,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.disable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho_connector_disable") == 1

    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho_connector_enable") == 1
    statuses = await _group_statuses(tenant_id=tenant_id)
    assert set(statuses.values()) == {"enabled"}
    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert all(enabled_state.values())


@pytest.mark.asyncio
async def test_enable_connector_cascades_to_ungrouped_descriptors() -> None:
    """#3681: a full ``enable_connector`` also enables ungrouped (``group_id IS NULL``) ops.

    The LLM grouping pass can leave descriptors ungrouped. Before #3681 the
    enable cascade filtered by ``group_id IN (<transitioned groups>)`` and
    skipped them, silently stranding ungrouped write/typed ops at
    default-deny. The cascade now also reaches ``group_id IS NULL`` rows in
    the connector scope, so a full enable enables the whole catalog — reads,
    writes, and typed ops alike.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=2,
        review_status="staged",
        op_is_enabled=False,
    )
    # Three descriptors the grouping pass never bucketed: a read, a write,
    # and a typed op (method NULL). All default-deny.
    await _add_ungrouped_op(tenant_id=tenant_id, op_id="GET:/api/v1/ungrouped/list", method="GET")
    await _add_ungrouped_op(
        tenant_id=tenant_id, op_id="POST:/api/v1/ungrouped/create", method="POST"
    )
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="typed:ungrouped.composite",
        method=None,
        source_kind="typed",
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    # Grouped ops flipped ...
    assert enabled_state["GET:/api/v1/group-0/0"] is True
    assert enabled_state["GET:/api/v1/group-0/1"] is True
    # ... and so did the ungrouped read, write, and typed op (#3681).
    assert enabled_state["GET:/api/v1/ungrouped/list"] is True
    assert enabled_state["POST:/api/v1/ungrouped/create"] is True
    assert enabled_state["typed:ungrouped.composite"] is True

    row = await _latest_audit_row(op_id="meho_connector_enable")
    payload: Any = row.payload
    # 2 grouped + 3 ungrouped all flipped in the one cascade.
    assert payload["ops_cascade_count"] == 5


@pytest.mark.asyncio
async def test_enable_connector_respects_operator_override_on_ungrouped_op() -> None:
    """#3681: an operator ``is_enabled=False`` override on an ungrouped op survives enable.

    Widening the cascade to ungrouped rows must not stomp a per-op operator
    override on one of them — the override-exclusion lookup was widened to
    ungrouped ops in the same change.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=1,
        review_status="staged",
        op_is_enabled=False,
    )
    await _add_ungrouped_op(tenant_id=tenant_id, op_id="GET:/api/v1/ungrouped/list", method="GET")
    await _add_ungrouped_op(
        tenant_id=tenant_id, op_id="POST:/api/v1/ungrouped/create", method="POST"
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    # Operator explicitly disables the ungrouped write.
    await service.edit_op(
        "vmware-rest-9.0",
        "POST:/api/v1/ungrouped/create",
        tenant_id=tenant_id,
        is_enabled=False,
    )
    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert enabled_state["POST:/api/v1/ungrouped/create"] is False, (
        "operator override on an ungrouped op clobbered by the enable cascade"
    )
    # The other ungrouped op and the grouped op still flipped.
    assert enabled_state["GET:/api/v1/ungrouped/list"] is True
    assert enabled_state["GET:/api/v1/group-0/0"] is True


@pytest.mark.asyncio
async def test_disable_connector_cascades_to_ungrouped_descriptors() -> None:
    """#3681: a full ``disable_connector`` also disables ungrouped ops.

    Kept symmetric with the enable cascade so a full disable locks down
    exactly what a full enable opened — otherwise enabling then disabling
    would leave ungrouped writes stranded in an enabled state.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=1,
        review_status="enabled",
        op_is_enabled=True,
    )
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="POST:/api/v1/ungrouped/create",
        method="POST",
        is_enabled=True,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.disable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert enabled_state["POST:/api/v1/ungrouped/create"] is False
    assert enabled_state["GET:/api/v1/group-0/0"] is False


# ---------------------------------------------------------------------------
# enable_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_group_transitions_one_group_only() -> None:
    """``enable_group`` flips one group + its child ops, leaves siblings staged."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=2,
        ops_per_group=2,
        review_status="staged",
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_group(
        "vmware-rest-9.0",
        "group-0",
        tenant_id=tenant_id,
    )

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert statuses["group-0"] == "enabled"
    assert statuses["group-1"] == "staged"
    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert enabled_state["GET:/api/v1/group-0/0"] is True
    assert enabled_state["GET:/api/v1/group-0/1"] is True
    assert enabled_state["GET:/api/v1/group-1/0"] is False

    assert await _count_audit_rows(op_id="meho_connector_enable_group") == 1


@pytest.mark.asyncio
async def test_enable_group_idempotent_on_already_enabled() -> None:
    """``enable_group`` re-run on an already-enabled group writes no audit row."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_id,
        group_count=1,
        ops_per_group=1,
        review_status="enabled",
        op_is_enabled=True,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    await service.enable_group(
        "vmware-rest-9.0",
        "group-0",
        tenant_id=tenant_id,
    )
    assert await _count_audit_rows(op_id="meho_connector_enable_group") == 0


# ---------------------------------------------------------------------------
# Tenant isolation + built-in role gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_access_yields_connector_not_found() -> None:
    """Tenant A's operator cannot mutate tenant B's connector."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b, group_count=1, ops_per_group=1)

    service = ReviewService(_make_operator(tenant_id=tenant_a))

    with pytest.raises(ConnectorNotFoundError):
        await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_b)

    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload("vmware-rest-9.0", tenant_b)

    # Tenant B's connector status is untouched.
    statuses = await _group_statuses(tenant_id=tenant_b)
    assert set(statuses.values()) == {"staged"}


@pytest.mark.asyncio
async def test_builtin_connector_requires_tenant_admin_role() -> None:
    """Non-admin operators get :class:`ConnectorNotFoundError` on built-in scope."""
    tenant_id = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,  # built-in
        group_count=1,
        ops_per_group=1,
        review_status="staged",
    )
    operator_role = _make_operator(
        tenant_id=tenant_id,
        role=TenantRole.OPERATOR,
    )
    service = ReviewService(operator_role)
    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload("vmware-rest-9.0", None)
    with pytest.raises(ConnectorNotFoundError):
        await service.enable_connector("vmware-rest-9.0", tenant_id=None)

    read_only = _make_operator(
        tenant_id=tenant_id,
        role=TenantRole.READ_ONLY,
    )
    service_ro = ReviewService(read_only)
    with pytest.raises(ConnectorNotFoundError):
        await service_ro.get_review_payload("vmware-rest-9.0", None)


@pytest.mark.asyncio
async def test_builtin_connector_accessible_to_platform_admin() -> None:
    """``platform_admin`` mutates built-in connectors; audit echoes operator's tenant."""
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,
        group_count=1,
        ops_per_group=2,
        review_status="staged",
    )
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = ReviewService(admin)

    await service.enable_connector("vmware-rest-9.0", tenant_id=None)

    statuses = await _group_statuses(tenant_id=None)
    assert set(statuses.values()) == {"enabled"}

    # Audit row carries the operator's tenant, not the affected
    # rows' (NULL) scope.
    row = await _latest_audit_row(op_id="meho_connector_enable")
    assert row.tenant_id == operator_tenant


@pytest.mark.asyncio
async def test_builtin_connector_enable_forbidden_without_platform_admin() -> None:
    """A plain ``tenant_admin`` mutating a built-in row is refused (403), not 404.

    The reported v0.35.0 defect flavour + its RBAC floor: a freshly
    ingested built-in (global, ``tenant_id IS NULL``) connector is
    resolvable via the tenant-preferring fall-back, but mutating it is a
    platform action. A ``tenant_admin`` without ``platform_admin`` gets a
    typed :class:`BuiltinConnectorWriteForbiddenError` (the route maps it
    to 403) rather than the historical
    :class:`ConnectorNotFoundError`/404 or a silent all-tenant write.
    Nothing transitions.
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,
        group_count=1,
        ops_per_group=2,
        review_status="staged",
    )
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
    )
    service = ReviewService(admin)

    # Addressed by the operator's own tenant (the REST route contract:
    # enable() passes operator.tenant_id, never null) — resolves to the
    # built-in row via the fall-back, then the platform gate refuses it.
    with pytest.raises(BuiltinConnectorWriteForbiddenError) as excinfo:
        await service.enable_connector("vmware-rest-9.0", tenant_id=operator_tenant)
    assert excinfo.value.connector_id == "vmware-rest-9.0"
    assert excinfo.value.detail["error"] == "builtin_connector_write_forbidden"

    # And the same refusal when the built-in scope is addressed explicitly.
    with pytest.raises(BuiltinConnectorWriteForbiddenError):
        await service.enable_connector("vmware-rest-9.0", tenant_id=None)

    statuses = await _group_statuses(tenant_id=None)
    assert set(statuses.values()) == {"staged"}


@pytest.mark.asyncio
async def test_builtin_connector_enable_platform_admin_via_tenant_fallback() -> None:
    """``platform_admin`` enables a built-in-only connector addressed by its own tenant.

    The exact fix for the reported defect: the REST ``/enable`` route
    always calls ``enable_connector(..., tenant_id=operator.tenant_id)``,
    so a built-in-only label must resolve through the tenant-preferring
    fall-back to the built-in row and enable it — no 404 — when the caller
    holds ``platform_admin``.
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,
        group_count=2,
        ops_per_group=2,
        review_status="staged",
    )
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = ReviewService(admin)

    await service.enable_connector("vmware-rest-9.0", tenant_id=operator_tenant)

    statuses = await _group_statuses(tenant_id=None)
    assert set(statuses.values()) == {"enabled"}
    row = await _latest_audit_row(op_id="meho_connector_enable")
    assert row.tenant_id == operator_tenant


@pytest.mark.asyncio
async def test_builtin_connector_disable_forbidden_without_platform_admin() -> None:
    """The transition gate covers ``disable`` too (shared ``_transition_connector``)."""
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,
        group_count=1,
        ops_per_group=1,
        review_status="enabled",
    )
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    service = ReviewService(admin)

    with pytest.raises(BuiltinConnectorWriteForbiddenError):
        await service.disable_connector("vmware-rest-9.0", tenant_id=operator_tenant)

    statuses = await _group_statuses(tenant_id=None)
    assert set(statuses.values()) == {"enabled"}


@pytest.mark.asyncio
async def test_builtin_edit_op_and_group_gated_on_platform_admin() -> None:
    """``edit_op`` / ``edit_group`` on a built-in row need ``platform_admin`` too."""
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,
        group_count=1,
        ops_per_group=1,
        review_status="staged",
    )
    tenant_admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    ta_service = ReviewService(tenant_admin)

    with pytest.raises(BuiltinConnectorWriteForbiddenError):
        await ta_service.edit_group(
            "vmware-rest-9.0",
            "group-0",
            tenant_id=operator_tenant,
            when_to_use="tenant admin should not reach the built-in row",
        )
    with pytest.raises(BuiltinConnectorWriteForbiddenError):
        await ta_service.edit_op(
            "vmware-rest-9.0",
            "GET:/api/v1/group-0/0",
            tenant_id=operator_tenant,
            is_enabled=True,
        )

    # platform_admin lands both edits.
    platform_admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    pa_service = ReviewService(platform_admin)
    await pa_service.edit_group(
        "vmware-rest-9.0",
        "group-0",
        tenant_id=operator_tenant,
        when_to_use="platform admin curated this built-in group",
    )
    warnings = await pa_service.edit_op(
        "vmware-rest-9.0",
        "GET:/api/v1/group-0/0",
        tenant_id=operator_tenant,
        is_enabled=True,
    )
    assert isinstance(warnings, list)
    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert builtin_state["GET:/api/v1/group-0/0"] is True


# ---------------------------------------------------------------------------
# Audit-row sanity checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_connector_id_format_surfaces_as_connector_not_found() -> None:
    """Unparseable connector_id strings raise :class:`ConnectorNotFoundError`."""
    tenant_id = uuid.uuid4()
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload("not-a-version-string", tenant_id)


@pytest.mark.asyncio
async def test_invalid_state_transition_class_renders_message() -> None:
    """:class:`InvalidStateTransitionError` builds a readable message string.

    No path through the public API can reach an
    ``enabled → staged`` transition (there is no API verb that
    requests ``staged`` as a target), so the exception's role is
    defensive — fired only if a future state value drifts outside
    the documented set. The test exercises the exception's
    ``__str__`` so its operator-facing message is regression-
    locked.
    """
    exc = InvalidStateTransitionError(
        current_status="enabled",
        requested_status="staged",
        group_key="vm-lifecycle",
    )
    assert "'enabled'" in str(exc)
    assert "'staged'" in str(exc)
    assert "vm-lifecycle" in str(exc)


@pytest.mark.asyncio
async def test_audit_rows_carry_service_method_marker() -> None:
    """Every service-emitted audit row carries ``method='SERVICE'``."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=1)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.edit_group(
        "vmware-rest-9.0",
        "group-0",
        tenant_id=tenant_id,
        name="renamed",
    )
    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.path.in_(
                    ["meho_connector_edit_group", "meho_connector_enable"],
                ),
            ),
        )
        rows = list(result.scalars().all())
    assert rows, "expected at least two service-level audit rows"
    for row in rows:
        assert row.method == "SERVICE", (
            f"audit row path={row.path!r} has method={row.method!r}, expected SERVICE"
        )


# ---------------------------------------------------------------------------
# enable_reads — bulk read-class enable path (G0.25-T7 #1749)
# ---------------------------------------------------------------------------


async def _seed_mixed_methods(
    *,
    tenant_id: uuid.UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
    review_status: str = "staged",
    op_is_enabled: bool = False,
) -> dict[str, str]:
    """Seed one group with one op per HTTP verb plus a typed (method=NULL) op.

    Returns ``{op_id: method}`` so a test can assert exactly which ops
    the bulk read-class enable should and should not have flipped. The
    read class is ``GET`` / ``HEAD``; ``POST`` / ``PUT`` / ``PATCH`` /
    ``DELETE`` are write-shaped; the typed op carries ``method=None`` /
    ``source_kind='typed'`` and must never be matched (it is not an
    ingested HTTP row).
    """
    sessionmaker = get_sessionmaker()
    ingested_methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    op_methods: dict[str, str] = {}
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key="mixed",
                name="Mixed",
                when_to_use="Use for mixed-verb ops.",
                review_status=review_status,
            ),
        )
        for method in ingested_methods:
            op_id = f"{method}:/api/v1/resource"
            op_methods[op_id] = method
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path="/api/v1/resource",
                    group_id=group_id,
                    summary=f"{method} resource",
                    is_enabled=op_is_enabled,
                ),
            )
        # A typed op (method NULL) — must never be touched by the
        # read-class filter even though it is a "read" by name.
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id="vmware.vm.list",
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="meho_backplane.connectors.vmware.ops.vm_list",
                group_id=group_id,
                summary="List VMs (typed)",
                is_enabled=op_is_enabled,
            ),
        )
        await session.commit()
    return op_methods


@pytest.mark.asyncio
async def test_enable_reads_flips_reads_leaves_writes_default_deny() -> None:
    """AC: GET/HEAD ingested ops flip to enabled; every write-class op stays false."""
    tenant_id = uuid.uuid4()
    op_methods = await _seed_mixed_methods(tenant_id=tenant_id, op_is_enabled=False)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)

    # Two read-class ingested ops flipped (GET + HEAD); the typed
    # method=NULL op is not counted.
    assert ops_enabled == 2
    state = await _ops_enabled_state(tenant_id=tenant_id)
    for op_id, method in op_methods.items():
        if method in ("GET", "HEAD"):
            assert state[op_id] is True, f"read-class {op_id} should be enabled"
        else:
            assert state[op_id] is False, f"write-class {op_id} must stay default-deny"
    # The typed (method NULL) op is never touched.
    assert state["vmware.vm.list"] is False


@pytest.mark.asyncio
async def test_enable_reads_writes_one_audit_row_with_count() -> None:
    """AC: exactly one ``meho_connector_enable_reads`` audit row carrying the count."""
    tenant_id = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=tenant_id, op_is_enabled=False)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)

    assert await _count_audit_rows(op_id="meho_connector_enable_reads") == 1
    row = await _latest_audit_row(op_id="meho_connector_enable_reads")
    payload: Any = row.payload
    assert payload["connector_id"] == "vmware-rest-9.0"
    assert payload["ops_enabled_count"] == 2
    assert row.method == "SERVICE"


@pytest.mark.asyncio
async def test_enable_reads_is_idempotent_no_second_audit_row() -> None:
    """AC: a re-run enables nothing new, returns 0, and writes no second audit row."""
    tenant_id = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=tenant_id, op_is_enabled=False)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    first = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)
    assert first == 2
    assert await _count_audit_rows(op_id="meho_connector_enable_reads") == 1

    second = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)
    assert second == 0
    assert await _count_audit_rows(op_id="meho_connector_enable_reads") == 1


@pytest.mark.asyncio
async def test_enable_reads_does_not_move_group_review_status() -> None:
    """``enable_reads`` flips per-op flags only — group review_status is untouched."""
    tenant_id = uuid.uuid4()
    await _seed_mixed_methods(
        tenant_id=tenant_id,
        review_status="staged",
        op_is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert set(statuses.values()) == {"staged"}, "group must stay staged"


@pytest.mark.asyncio
async def test_enable_reads_on_connector_without_groups_flips_read_ops() -> None:
    """#3870: enable-reads shares the review resolver, so it finds a group-less connector too."""
    tenant_id = uuid.uuid4()
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/ungrouped/list",
        method="GET",
        is_enabled=False,
    )
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="POST:/api/v1/ungrouped/create",
        method="POST",
        is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)

    assert enabled == 1
    state = await _ops_enabled_state(tenant_id=tenant_id)
    assert state["GET:/api/v1/ungrouped/list"] is True
    assert state["POST:/api/v1/ungrouped/create"] is False


@pytest.mark.asyncio
async def test_edit_op_disables_read_on_connector_without_groups() -> None:
    """#3870: an op enabled by enable-reads on a group-less connector can be disabled again.

    ``edit_op`` must find the tenant's op rows without any group row,
    the same way ``enable_reads`` does. Before the fix the per-op edit
    probed group rows only and raised ``ConnectorNotFoundError``, so the
    op stayed enabled.
    """
    tenant_id = uuid.uuid4()
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/ungrouped/list",
        method="GET",
        is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    assert await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id) == 1

    await service.edit_op(
        "vmware-rest-9.0",
        "GET:/api/v1/ungrouped/list",
        tenant_id=tenant_id,
        is_enabled=False,
    )

    state = await _ops_enabled_state(tenant_id=tenant_id)
    assert state["GET:/api/v1/ungrouped/list"] is False


@pytest.mark.asyncio
async def test_edit_op_disables_tenant_read_when_only_builtin_has_groups() -> None:
    """#3870: a tenant op-only row is edited in tenant scope even when a built-in row has groups.

    Before the fix the per-op edit saw no tenant group rows and slid to
    the built-in scope: a ``tenant_admin`` was refused with
    ``BuiltinConnectorWriteForbiddenError`` and the tenant op stayed
    enabled. The built-in row must stay untouched.
    """
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=None, group_count=1, ops_per_group=1, op_is_enabled=False)
    await _add_ungrouped_op(
        tenant_id=tenant_id,
        op_id="GET:/api/v1/tenant-only",
        method="GET",
        is_enabled=False,
    )
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id, prefer="tenant")
    assert enabled == 1

    await service.edit_op(
        "vmware-rest-9.0",
        "GET:/api/v1/tenant-only",
        tenant_id=tenant_id,
        is_enabled=False,
    )

    tenant_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert tenant_state == {"GET:/api/v1/tenant-only": False}
    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert builtin_state == {"GET:/api/v1/group-0/0": False}


@pytest.mark.asyncio
async def test_enable_reads_unknown_connector_raises_not_found() -> None:
    """A connector triple with no rows yields :class:`ConnectorNotFoundError`."""
    tenant_id = uuid.uuid4()
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    with pytest.raises(ConnectorNotFoundError):
        await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_enable_reads_cross_tenant_yields_not_found() -> None:
    """Tenant A's operator cannot enable-reads tenant B's connector; B untouched."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=tenant_b, op_is_enabled=False)
    service = ReviewService(_make_operator(tenant_id=tenant_a))

    with pytest.raises(ConnectorNotFoundError):
        await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_b)

    # Tenant B's read ops remain default-deny.
    state = await _ops_enabled_state(tenant_id=tenant_b)
    assert not any(state.values())


@pytest.mark.asyncio
async def test_enable_reads_builtin_requires_tenant_admin() -> None:
    """Non-admin operators get :class:`ConnectorNotFoundError` on built-in scope."""
    tenant_id = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    operator_role = _make_operator(tenant_id=tenant_id, role=TenantRole.OPERATOR)
    service = ReviewService(operator_role)
    with pytest.raises(ConnectorNotFoundError):
        await service.enable_reads("vmware-rest-9.0", tenant_id=None)
    # Nothing flipped under the built-in scope.
    state = await _ops_enabled_state(tenant_id=None)
    assert not any(state.values())


@pytest.mark.asyncio
async def test_enable_reads_builtin_accessible_to_platform_admin() -> None:
    """``platform_admin`` bulk-enables built-in reads; audit echoes operator's tenant."""
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = ReviewService(admin)

    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=None)
    assert ops_enabled == 2

    row = await _latest_audit_row(op_id="meho_connector_enable_reads")
    # Audit row carries the operator's tenant, not the affected rows'
    # (NULL) scope.
    assert row.tenant_id == operator_tenant


@pytest.mark.asyncio
async def test_enable_reads_only_flips_disabled_reads() -> None:
    """An already-enabled read is left alone; the count reflects only true flips."""
    tenant_id = uuid.uuid4()
    op_methods = await _seed_mixed_methods(tenant_id=tenant_id, op_is_enabled=False)
    # Pre-enable the GET op via the per-op edit path so only HEAD is
    # left to flip.
    get_op_id = next(op for op, m in op_methods.items() if m == "GET")
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    await service.edit_op(
        "vmware-rest-9.0",
        get_op_id,
        tenant_id=tenant_id,
        is_enabled=True,
    )

    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)
    assert ops_enabled == 1  # only HEAD flipped; GET was already enabled

    state = await _ops_enabled_state(tenant_id=tenant_id)
    assert state[get_op_id] is True
    head_op_id = next(op for op, m in op_methods.items() if m == "HEAD")
    assert state[head_op_id] is True


# ---------------------------------------------------------------------------
# Shared scope resolution — /review and /enable-reads resolve the SAME row
# (G0.26-T1 #1801)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_reads_falls_back_to_builtin_for_operator_tenant() -> None:
    """G0.26-T1 (#1801): enable_reads honours the #1135 global fallback.

    The exact dogfood footgun: a connector that exists only as a
    built-in (``tenant_id IS NULL``) row used to 404 on
    ``/enable-reads`` while ``/review`` returned 200 (the read/write
    asymmetry). Now both share the resolver, so a ``tenant_admin``
    enabling reads on a global-only label flips the **built-in** read
    ops instead of 404'ing.
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    # platform_admin is required to WRITE the built-in scope; the
    # fallback resolves to tenant_id=None.
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = ReviewService(admin)

    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=operator_tenant)

    # GET + HEAD on the built-in rows flipped — proving the resolver
    # fell back to the global scope rather than raising not-found.
    assert ops_enabled == 2
    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert builtin_state["GET:/api/v1/resource"] is True
    assert builtin_state["HEAD:/api/v1/resource"] is True
    assert builtin_state["POST:/api/v1/resource"] is False


@pytest.mark.asyncio
async def test_enable_reads_builtin_fallback_forbidden_without_platform_admin() -> None:
    """A plain ``tenant_admin`` hitting the built-in read fall-back is refused (403).

    Same shape as the transition gate: the #1135 fall-back still resolves
    the built-in row for a tenant-addressed label, but flipping its reads
    is a platform write, so a ``tenant_admin`` without ``platform_admin``
    gets :class:`BuiltinConnectorWriteForbiddenError` and nothing flips.
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    service = ReviewService(admin)

    with pytest.raises(BuiltinConnectorWriteForbiddenError):
        await service.enable_reads("vmware-rest-9.0", tenant_id=operator_tenant)

    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert not any(builtin_state.values())


@pytest.mark.asyncio
async def test_enable_reads_ambiguous_tenant_and_builtin_raises() -> None:
    """G0.26-T1 (#1801): enable_reads raises on a tenant+built-in ambiguous label.

    Symmetric with the read path: when both a tenant row and a
    built-in row exist for the label, enable_reads raises
    :class:`AmbiguousConnectorScopeError` rather than silently flipping
    one scope's reads. Nothing flips on either scope.
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=operator_tenant, op_is_enabled=False)
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    service = ReviewService(admin)

    with pytest.raises(AmbiguousConnectorScopeError) as excinfo:
        await service.enable_reads("vmware-rest-9.0", tenant_id=operator_tenant)

    assert [c.tenant_id for c in excinfo.value.candidates] == [None, operator_tenant]
    # The raise happens before any UPDATE — both scopes untouched.
    tenant_state = await _ops_enabled_state(tenant_id=operator_tenant)
    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert not any(tenant_state.values())
    assert not any(builtin_state.values())


@pytest.mark.asyncio
async def test_enable_reads_prefer_tenant_applies_to_tenant_row() -> None:
    """#2029: ``prefer='tenant'`` flips the tenant row's reads, not the built-in.

    Both scopes hold an identical mixed-verb surface; ``prefer='tenant'``
    must flip GET+HEAD on the operator's tenant row and leave the
    built-in scope wholly untouched (no silent cross-scope flip).
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=operator_tenant, op_is_enabled=False)
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    service = ReviewService(admin)

    ops_enabled = await service.enable_reads(
        "vmware-rest-9.0",
        tenant_id=operator_tenant,
        prefer="tenant",
    )

    assert ops_enabled == 2
    tenant_state = await _ops_enabled_state(tenant_id=operator_tenant)
    assert tenant_state["GET:/api/v1/resource"] is True
    assert tenant_state["HEAD:/api/v1/resource"] is True
    assert tenant_state["POST:/api/v1/resource"] is False
    # The built-in scope was never touched.
    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert not any(builtin_state.values())


@pytest.mark.asyncio
async def test_enable_reads_prefer_builtin_applies_to_builtin_row() -> None:
    """#2029: ``prefer='builtin'`` flips the built-in row's reads (platform_admin)."""
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=operator_tenant, op_is_enabled=False)
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = ReviewService(admin)

    ops_enabled = await service.enable_reads(
        "vmware-rest-9.0",
        tenant_id=operator_tenant,
        prefer="builtin",
    )

    assert ops_enabled == 2
    builtin_state = await _ops_enabled_state(tenant_id=None)
    assert builtin_state["GET:/api/v1/resource"] is True
    assert builtin_state["HEAD:/api/v1/resource"] is True
    assert builtin_state["POST:/api/v1/resource"] is False
    # The tenant scope was never touched.
    tenant_state = await _ops_enabled_state(tenant_id=operator_tenant)
    assert not any(tenant_state.values())


@pytest.mark.asyncio
async def test_review_and_enable_reads_resolve_same_row_global_only() -> None:
    """AC1 (#1801): read + write resolve the identical row for a global-only label.

    Seeds a built-in-only connector, then proves ``get_review_payload``
    renders the built-in scope (``tenant_id is None``) *and*
    ``enable_reads`` flips that same built-in scope's read ops — one
    shared resolution path, one row, for both the read and the write.
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    # platform_admin so the WRITE half is permitted on the built-in row;
    # the parity property under test (read + write resolve the SAME row)
    # is unaffected by the orthogonal platform gate.
    admin = _make_operator(
        tenant_id=operator_tenant,
        role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )
    service = ReviewService(admin)

    # Read path resolves to the built-in scope.
    payload = await service.get_review_payload("vmware-rest-9.0", operator_tenant)
    assert payload.tenant_id is None

    # Write path resolves to the same built-in scope (flips its reads).
    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=operator_tenant)
    assert ops_enabled == 2
    assert (await _ops_enabled_state(tenant_id=None))["GET:/api/v1/resource"] is True


@pytest.mark.asyncio
async def test_review_and_enable_reads_resolve_same_row_tenant_only() -> None:
    """AC1 (#1801): read + write resolve the identical row for a tenant-only label.

    The tenant-curated-only case: ``get_review_payload`` renders the
    tenant scope and ``enable_reads`` flips that same tenant scope's
    reads, with no built-in row in play.
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=operator_tenant, op_is_enabled=False)
    operator = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    service = ReviewService(operator)

    payload = await service.get_review_payload("vmware-rest-9.0", operator_tenant)
    assert payload.tenant_id == operator_tenant

    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=operator_tenant)
    assert ops_enabled == 2
    tenant_state = await _ops_enabled_state(tenant_id=operator_tenant)
    assert tenant_state["GET:/api/v1/resource"] is True
    assert tenant_state["HEAD:/api/v1/resource"] is True


# ---------------------------------------------------------------------------
# Group-truth review advice (#3671)
# ---------------------------------------------------------------------------

# (op_id, summary, safety_level, requires_approval, is_enabled)
_OpSeed = tuple[str, str, str, bool, bool]


async def _seed_group_with_ops(
    *,
    tenant_id: uuid.UUID,
    when_to_use: str,
    ops: list[_OpSeed],
    group_key: str = "group-0",
) -> None:
    """Insert one ``vmware-rest-9.0`` group holding *ops* as given."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                group_key=group_key,
                name="Group 0",
                when_to_use=when_to_use,
                review_status="enabled",
            )
        )
        for op_id, summary, safety_level, requires_approval, is_enabled in ops:
            method, _, path = op_id.partition(":")
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    group_id=group_id,
                    summary=summary,
                    safety_level=safety_level,
                    requires_approval=requires_approval,
                    is_enabled=is_enabled,
                )
            )
        await session.commit()


async def _group_advisory(tenant_id: uuid.UUID) -> Any:
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    payload = await service.get_review_payload("vmware-rest-9.0", tenant_id)
    assert len(payload.groups) == 1
    return payload.groups[0].advisory


@pytest.mark.asyncio
async def test_group_advisory_safety_distribution_counts_enabled_ops_only() -> None:
    """The distribution counts enabled ops per level, approvals, and the total."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Manage virtual machines: list, create, and delete VMs.",
        ops=[
            ("GET:/vcenter/vm", "List VMs", "safe", False, True),
            ("GET:/vcenter/vm/{vm}", "Get VM", "safe", False, True),
            ("POST:/vcenter/vm", "Create VM", "caution", False, True),
            ("DELETE:/vcenter/vm/{vm}", "Delete VM", "dangerous", True, True),
            ("POST:/vcenter/vm/{vm}/destroy", "Destroy VM", "destructive", True, True),
            # Disabled: must not be counted anywhere.
            ("DELETE:/vcenter/vm/{vm}/disk", "Delete VM disk", "dangerous", True, False),
            ("GET:/vcenter/vm/{vm}/power", "Get VM power", "safe", False, False),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.distribution.model_dump() == {
        "enabled_op_count": 5,
        "safe": 2,
        "caution": 1,
        "dangerous": 1,
        "destructive": 1,
        "requires_approval_count": 2,
    }
    assert advisory.flags == []


@pytest.mark.asyncio
async def test_group_advisory_safety_distribution_is_zero_without_enabled_ops() -> None:
    """A staged group with no enabled op reports an all-zero distribution."""
    tenant_id = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_id, group_count=1, ops_per_group=3)

    advisory = await _group_advisory(tenant_id)

    assert advisory.distribution.enabled_op_count == 0
    assert advisory.distribution.safe == 0
    assert advisory.distribution.requires_approval_count == 0


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_read_only_claim_contradicted() -> None:
    """Text that says read-only is flagged when enabled ops need approval."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Read-only inventory of virtual machines.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("DELETE:/vcenter/vm/{vm}", "Delete a virtual machine", "dangerous", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert len(advisory.flags) >= 1
    assert any("read-only" in flag and "dangerous: 1" in flag for flag in advisory.flags)


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_read_only_claim_safe_op_needs_approval() -> None:
    """A safe op that needs approval breaks a read-only claim and shows its count."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Read-only VM list.",
        ops=[
            ("GET:/vcenter/vm", "List VMs", "safe", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    read_only = [flag for flag in advisory.flags if "read-only" in flag]
    assert len(read_only) == 1
    assert "1 op(s) are not safe or need approval (need approval: 1)." in read_only[0]
    assert "()" not in read_only[0]


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_all_destructive_claim_contradicted() -> None:
    """Text that says every op is destructive is flagged when some are lower."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="All VM operations in this group are destructive.",
        ops=[
            ("POST:/vcenter/vm/{vm}/destroy", "Destroy VM", "destructive", True, True),
            ("GET:/vcenter/vm", "List VMs", "safe", False, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert any("destructive" in flag and "safe: 1" in flag for flag in advisory.flags)


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_all_dangerous_claim_understates_risk() -> None:
    """Text that says every op is dangerous is flagged when one is destructive."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="All virtual machine operations in this group are dangerous.",
        ops=[
            ("DELETE:/vcenter/vm/{vm}", "Delete a virtual machine", "dangerous", True, True),
            ("POST:/vcenter/vm/{vm}/destroy", "Destroy VM", "destructive", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert len(advisory.flags) == 1
    assert "all ops are dangerous" in advisory.flags[0]
    assert "destructive: 1" in advisory.flags[0]


@pytest.mark.asyncio
async def test_group_advisory_all_non_destructive_claim_is_not_a_destructive_claim() -> None:
    """Text that says every op is non-destructive raises no flag over safe ops."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="All virtual machine operations in this group are non-destructive.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("GET:/vcenter/vm/{vm}", "Get one virtual machine", "safe", False, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == []


@pytest.mark.asyncio
async def test_group_advisory_non_destructive_claim_allows_caution_ops() -> None:
    """Text that says non-destructive is not a read-only claim; caution ops pass."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Non-destructive updates to virtual machine settings.",
        ops=[
            ("GET:/vcenter/vm/{vm}", "Get one virtual machine", "safe", False, True),
            ("PATCH:/vcenter/vm/{vm}", "Update a virtual machine", "caution", False, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == []


@pytest.mark.asyncio
async def test_group_advisory_non_destructive_claim_contradicted_by_destructive_op() -> None:
    """Text that says non-destructive is flagged when an op is destructive."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Non-destructive updates to virtual machine settings.",
        ops=[
            ("PATCH:/vcenter/vm/{vm}", "Update a virtual machine", "caution", False, True),
            ("POST:/vcenter/vm/{vm}/destroy", "Destroy VM", "destructive", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert len(advisory.flags) == 1
    assert "non-destructive" in advisory.flags[0]
    assert "1 op(s) are destructive" in advisory.flags[0]


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_no_shared_words() -> None:
    """Text that shares no word with the enabled ops is flagged."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Use this for certificate rotation and trust stores.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("GET:/vcenter/vm/{vm}", "Get one virtual machine", "safe", False, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert len(advisory.flags) == 1
    assert "shares no word" in advisory.flags[0]


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_none_when_text_matches_ops() -> None:
    """Text that matches its enabled ops raises no flag."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Read-only listing of virtual machines and their power state.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("GET:/vcenter/vm/{vm}/power", "Get the power state", "safe", False, True),
            # Disabled, so it does not break the read-only claim.
            ("DELETE:/vcenter/vm/{vm}", "Delete a virtual machine", "dangerous", True, False),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == []


@pytest.mark.asyncio
async def test_group_advisory_prose_mismatch_checks_all_ops_when_none_enabled() -> None:
    """A staged group with no enabled op is checked against all of its ops."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Read-only inventory of virtual machines.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, False),
            ("DELETE:/vcenter/vm/{vm}", "Delete a virtual machine", "dangerous", True, False),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.distribution.enabled_op_count == 0
    assert len(advisory.flags) == 1
    assert "read-only" in advisory.flags[0]
    assert "No op is enabled yet" in advisory.flags[0]


@pytest.mark.asyncio
async def test_group_advisory_flags_approval_claim_without_all() -> None:
    """Flag "Write operations are approval-required." when most changes run at once."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Manage virtual machines. Write operations are approval-required.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("POST:/vcenter/vm", "Create a virtual machine", "caution", False, True),
            ("PATCH:/vcenter/vm/{vm}", "Update a virtual machine", "caution", False, True),
            ("DELETE:/vcenter/vm/{vm}", "Delete a virtual machine", "dangerous", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == [
        "The text says changes need approval, but 2 of 3 change op(s) do not."
    ]


@pytest.mark.asyncio
async def test_group_advisory_change_approval_claim_is_true_with_a_safe_read() -> None:
    """The change claim is about the write only, so no flag."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Manage virtual machines. Every change requires approval.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("POST:/vcenter/vm", "Create a virtual machine", "caution", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == []


@pytest.mark.asyncio
async def test_group_advisory_all_ops_approval_claim_is_false_with_a_safe_read() -> None:
    """The all-ops claim counts the read, so it is flagged."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Manage virtual machines. All operations are approval-required.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("POST:/vcenter/vm", "Create a virtual machine", "caution", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == ["The text says all ops need approval, but 1 op(s) do not."]


@pytest.mark.asyncio
async def test_group_advisory_approval_claim_that_names_reads_counts_the_read() -> None:
    """A claim that names reads and writes is about the read too, so it is flagged."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="Manage virtual machines. All read and write operations require approval.",
        ops=[
            ("GET:/vcenter/vm", "List virtual machines", "safe", False, True),
            ("POST:/vcenter/vm", "Create a virtual machine", "caution", True, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == ["The text says all ops need approval, but 1 op(s) do not."]


@pytest.mark.asyncio
async def test_group_advisory_all_approval_claim_is_flagged_once() -> None:
    """An "all ... require approval" sentence gives one flag, not two."""
    tenant_id = uuid.uuid4()
    await _seed_group_with_ops(
        tenant_id=tenant_id,
        when_to_use="All virtual machine operations require approval.",
        ops=[
            ("POST:/vcenter/vm", "Create a virtual machine", "caution", False, True),
        ],
    )

    advisory = await _group_advisory(tenant_id)

    assert advisory.flags == ["The text says all ops need approval, but 1 op(s) do not."]


# ---------------------------------------------------------------------------
# Re-ingest keeps operator overrides; a breaking change is a new version (#3671)
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_connector_registry() -> Iterator[None]:
    """Reset the connector registry around a test that ingests a spec."""
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def _stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _pet_proto(
    op_id: str,
    *,
    summary: str,
    parameter_schema: dict[str, Any] | None = None,
) -> EndpointDescriptorProto:
    method, _, path = op_id.partition(":")
    return EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path,
        summary=summary,
        description=summary,
        tags=["pets"],
        parameter_schema=parameter_schema or {"type": "object", "properties": {}},
        response_schema={"type": "object"},
        safety_level="safe" if method == "GET" else "caution",
        requires_approval=False,
    )


_PET_OPS = [
    _pet_proto("GET:/pets", summary="List pets"),
    _pet_proto("POST:/pets", summary="Add a pet"),
]


async def _load_pet_rows(tenant_id: uuid.UUID) -> dict[str, EndpointDescriptor]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == "petstore",
            )
        )
        return {row.op_id: row for row in result.scalars().all()}


async def _load_group(tenant_id: uuid.UUID | None, product: str, group_key: str) -> OperationGroup:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.product == product,
            OperationGroup.group_key == group_key,
        )
        if tenant_id is None:
            stmt = stmt.where(OperationGroup.tenant_id.is_(None))
        else:
            stmt = stmt.where(OperationGroup.tenant_id == tenant_id)
        return (await session.execute(stmt)).scalar_one()


async def _group_pet_ops(tenant_id: uuid.UUID) -> None:
    """Do what the grouping pass does: one staged group holding both pet ops."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product="petstore",
                version="1.0",
                impl_id="petstore-rest",
                group_key="pets",
                name="Pets",
                when_to_use="Manage pets: list pets and add a pet.",
                review_status="staged",
            )
        )
        rows = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == "petstore",
            )
        )
        for row in rows.scalars().all():
            row.group_id = group_id
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_connector_registry")
async def test_override_preserved_on_reingest_of_same_spec(
    _stub_embedding_service: AsyncMock,
) -> None:
    """A same-spec re-ingest keeps every operator edit byte-identical."""
    tenant_id = uuid.uuid4()

    async def ingest() -> None:
        await register_ingested_operations(
            product="petstore",
            version="1.0",
            impl_id="petstore-rest",
            spec_source="petstore.yaml",
            operations=_PET_OPS,
            tenant_id=tenant_id,
            embedding_service=_stub_embedding_service,
        )

    await ingest()
    await _group_pet_ops(tenant_id)

    # Operator review: enable the connector, then curate.
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    await service.enable_connector("petstore-rest-1.0", tenant_id=tenant_id)
    await service.edit_op(
        "petstore-rest-1.0",
        "GET:/pets",
        tenant_id=tenant_id,
        custom_description="Operator text: lists every pet in the shop.",
    )
    await service.edit_op("petstore-rest-1.0", "POST:/pets", tenant_id=tenant_id, is_enabled=False)
    await service.edit_group(
        "petstore-rest-1.0",
        "pets",
        tenant_id=tenant_id,
        when_to_use="Operator text: pet inventory and adding new pets.",
    )
    # ``custom_notes`` has no review verb; an operator sets it on the row.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.op_id == "GET:/pets",
            )
        )
        rows.scalar_one().custom_notes = "Operator note: slow on big shops."
        await session.commit()

    def snapshot(rows: dict[str, EndpointDescriptor]) -> dict[str, tuple[Any, ...]]:
        return {
            op_id: (row.custom_description, row.custom_notes, row.is_enabled, row.group_id)
            for op_id, row in rows.items()
        }

    before_ops = snapshot(await _load_pet_rows(tenant_id))
    before_group = await _load_group(tenant_id, "petstore", "pets")

    await ingest()
    # The grouping pass that follows an ingest has nothing to do: every op
    # is already in a group, so it never calls the LLM.
    llm_client = AsyncMock()
    grouping = await run_llm_grouping(
        llm_client=llm_client,
        operator_sub="test-operator",
        operator_tenant_id=tenant_id,
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        tenant_id=tenant_id,
    )
    assert grouping.groups_created == 0
    assert llm_client.mock_calls == []

    after_ops = snapshot(await _load_pet_rows(tenant_id))
    after_group = await _load_group(tenant_id, "petstore", "pets")
    assert after_ops == before_ops
    assert after_ops["GET:/pets"][:3] == (
        "Operator text: lists every pet in the shop.",
        "Operator note: slow on big shops.",
        True,
    )
    assert after_ops["POST:/pets"][2] is False
    assert after_group.review_status == before_group.review_status == "enabled"
    assert after_group.when_to_use == before_group.when_to_use
    assert after_group.when_to_use == "Operator text: pet inventory and adding new pets."


async def _typed_pets_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


@pytest.mark.asyncio
async def test_override_preserved_on_reingest_typed_group_when_to_use(
    _stub_embedding_service: AsyncMock,
) -> None:
    """Typed registration does not write its ``when_to_use`` over an operator edit."""

    async def register() -> None:
        await register_typed_operation(
            product="typedpets",
            version="1.x",
            impl_id="typedpets",
            op_id="typedpets.pet.list",
            handler=_typed_pets_handler,
            summary="List pets.",
            description="List pets.",
            parameter_schema={"type": "object"},
            group_key="pets",
            when_to_use="Shipped text: list pets.",
            embedding_service=_stub_embedding_service,
        )

    await register()
    service = ReviewService(_make_operator(tenant_id=uuid.uuid4(), platform_admin=True))
    await service.edit_group(
        "typedpets-1.x",
        "pets",
        tenant_id=None,
        when_to_use="Operator text: list every pet.",
    )

    await register()

    group = await _load_group(None, "typedpets", "pets")
    assert group.when_to_use == "Operator text: list every pet."


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_connector_registry")
async def test_breaking_change_new_version_identity_coexists(
    _stub_embedding_service: AsyncMock,
) -> None:
    """A breaking change ships as a new version; both versions stay resolvable."""
    tenant_id = uuid.uuid4()
    old_schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    new_schema = {
        "type": "object",
        "properties": {"page_size": {"type": "integer"}},
        "required": ["page_size"],
    }
    for version, schema in (("1.0", old_schema), ("2.0", new_schema)):
        # A new version needs a connector class that covers it. Ingest
        # refuses a version label no registered class accepts.
        ensure_connector_class_registered(
            product="petstore", version=version, impl_id="petstore-rest", base_url=None
        )
        await register_ingested_operations(
            product="petstore",
            version=version,
            impl_id="petstore-rest",
            spec_source=f"petstore-{version}.yaml",
            operations=[_pet_proto("GET:/pets", summary="List pets", parameter_schema=schema)],
            tenant_id=tenant_id,
            embedding_service=_stub_embedding_service,
        )
        await ReviewService(_make_operator(tenant_id=tenant_id)).edit_op(
            f"petstore-rest-{version}", "GET:/pets", tenant_id=tenant_id, is_enabled=True
        )

    old = await lookup_descriptor(
        tenant_id=tenant_id,
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        op_id="GET:/pets",
    )
    new = await lookup_descriptor(
        tenant_id=tenant_id,
        product="petstore",
        version="2.0",
        impl_id="petstore-rest",
        op_id="GET:/pets",
    )
    assert old is not None and new is not None
    assert old.id != new.id
    assert old.parameter_schema == old_schema
    assert new.parameter_schema == new_schema


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_connector_registry")
async def test_breaking_change_new_version_identity_same_key_other_spec_collides(
    _stub_embedding_service: AsyncMock,
) -> None:
    """The same natural key from a different spec raises, and leaves the row as it was."""
    tenant_id = uuid.uuid4()
    original_schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[_pet_proto("GET:/pets", summary="List pets", parameter_schema=original_schema)],
        tenant_id=tenant_id,
        embedding_service=_stub_embedding_service,
    )

    with pytest.raises(OpIdCollision) as exc_info:
        await register_ingested_operations(
            product="petstore",
            version="1.0",
            impl_id="petstore-rest",
            spec_source="petstore-v2.yaml",
            operations=[
                _pet_proto(
                    "GET:/pets",
                    summary="List pets, paged",
                    parameter_schema={"type": "object", "required": ["page_size"]},
                )
            ],
            tenant_id=tenant_id,
            embedding_service=_stub_embedding_service,
        )

    assert exc_info.value.existing_spec_source == "petstore.yaml"
    assert exc_info.value.incoming_spec_source == "petstore-v2.yaml"
    rows = await _load_pet_rows(tenant_id)
    assert rows["GET:/pets"].parameter_schema == original_schema
    assert rows["GET:/pets"].summary == "List pets"
