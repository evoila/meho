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

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import (
    ConnectorNotFoundError,
    ConnectorReviewPayload,
    InvalidStateTransitionError,
    ReviewService,
    parse_connector_id,
)
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
) -> Operator:
    """Build a frozen :class:`Operator` with default test fields."""
    return Operator(
        sub=sub or f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=role,
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

    assert await _count_audit_rows(op_id="meho.connector.edit_group") == 1
    row = await _latest_audit_row(op_id="meho.connector.edit_group")
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
        assert op_row.safety_level == "dangerous"
        assert op_row.requires_approval is True
        assert op_row.is_enabled is False

    assert await _count_audit_rows(op_id="meho.connector.edit_op") == 1
    row = await _latest_audit_row(op_id="meho.connector.edit_op")
    payload: Any = row.payload
    assert payload["op_id"] == target_op
    assert sorted(payload["fields_updated"]) == sorted(
        ["custom_description", "safety_level", "requires_approval", "is_enabled"],
    )
    assert payload["is_enabled_set_to"] is False


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

    assert await _count_audit_rows(op_id="meho.connector.enable") == 1
    row = await _latest_audit_row(op_id="meho.connector.enable")
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
    assert await _count_audit_rows(op_id="meho.connector.enable") == 1

    # Second call — no-op.
    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho.connector.enable") == 1


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

    assert await _count_audit_rows(op_id="meho.connector.disable") == 1


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
    assert await _count_audit_rows(op_id="meho.connector.disable") == 0


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
    assert await _count_audit_rows(op_id="meho.connector.disable") == 1

    await service.enable_connector("vmware-rest-9.0", tenant_id=tenant_id)
    assert await _count_audit_rows(op_id="meho.connector.enable") == 1
    statuses = await _group_statuses(tenant_id=tenant_id)
    assert set(statuses.values()) == {"enabled"}
    enabled_state = await _ops_enabled_state(tenant_id=tenant_id)
    assert all(enabled_state.values())


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

    assert await _count_audit_rows(op_id="meho.connector.enable_group") == 1


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
    assert await _count_audit_rows(op_id="meho.connector.enable_group") == 0


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
async def test_builtin_connector_accessible_to_tenant_admin() -> None:
    """``tenant_admin`` mutates built-in connectors; audit echoes operator's tenant."""
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

    await service.enable_connector("vmware-rest-9.0", tenant_id=None)

    statuses = await _group_statuses(tenant_id=None)
    assert set(statuses.values()) == {"enabled"}

    # Audit row carries the operator's tenant, not the affected
    # rows' (NULL) scope.
    row = await _latest_audit_row(op_id="meho.connector.enable")
    assert row.tenant_id == operator_tenant


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
                    ["meho.connector.edit_group", "meho.connector.enable"],
                ),
            ),
        )
        rows = list(result.scalars().all())
    assert rows, "expected at least two service-level audit rows"
    for row in rows:
        assert row.method == "SERVICE", (
            f"audit row path={row.path!r} has method={row.method!r}, expected SERVICE"
        )
