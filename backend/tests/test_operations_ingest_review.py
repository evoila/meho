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
    AmbiguousConnectorScopeError,
    ConnectorNotFoundError,
    ConnectorReviewPayload,
    EditOpWarning,
    InvalidStateTransitionError,
    ReviewService,
    ensure_connector_class_registered,
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

    row = await _latest_audit_row(op_id="meho.connector.edit_op")
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
    assert await _count_audit_rows(op_id="meho.connector.edit_op") == 1


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
    """AC: exactly one ``meho.connector.enable_reads`` audit row carrying the count."""
    tenant_id = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=tenant_id, op_is_enabled=False)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)

    assert await _count_audit_rows(op_id="meho.connector.enable_reads") == 1
    row = await _latest_audit_row(op_id="meho.connector.enable_reads")
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
    assert await _count_audit_rows(op_id="meho.connector.enable_reads") == 1

    second = await service.enable_reads("vmware-rest-9.0", tenant_id=tenant_id)
    assert second == 0
    assert await _count_audit_rows(op_id="meho.connector.enable_reads") == 1


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
async def test_enable_reads_builtin_accessible_to_tenant_admin() -> None:
    """``tenant_admin`` bulk-enables built-in reads; audit echoes operator's tenant."""
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
    service = ReviewService(admin)

    ops_enabled = await service.enable_reads("vmware-rest-9.0", tenant_id=None)
    assert ops_enabled == 2

    row = await _latest_audit_row(op_id="meho.connector.enable_reads")
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
    # tenant_admin is required to act on the built-in scope; the
    # fallback resolves to tenant_id=None.
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
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
async def test_review_and_enable_reads_resolve_same_row_global_only() -> None:
    """AC1 (#1801): read + write resolve the identical row for a global-only label.

    Seeds a built-in-only connector, then proves ``get_review_payload``
    renders the built-in scope (``tenant_id is None``) *and*
    ``enable_reads`` flips that same built-in scope's read ops — one
    shared resolution path, one row, for both the read and the write.
    """
    operator_tenant = uuid.uuid4()
    await _seed_mixed_methods(tenant_id=None, op_is_enabled=False)
    admin = _make_operator(tenant_id=operator_tenant, role=TenantRole.TENANT_ADMIN)
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
