# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An old tenant connector copy under a former product name (#4889).

Scenario: a tenant ingested the vRLI connector before v0.17.0, when its
product was still called ``vcf-logs``. Those rows share the id
``vrli-rest-9.0`` with the built-in copy (product ``vrli``) but not the
product, so every product-scoped lookup misses them and dispatch never
uses them. The service must let the operator review and delete that
copy, must not fall back to the built-in row for enable / disable /
edits, and the listing must not call the built-in row shadowed.
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
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.ingest import (
    AmbiguousConnectorScopeError,
    ConnectorNotFoundError,
    LegacyTenantConnectorCopyError,
    ReviewService,
    list_ingested_connectors,
)
from meho_backplane.settings import get_settings

_VERSION = "9.0"
_IMPL_ID = "vrli-rest"
_PRODUCT = "vrli"
_LEGACY_PRODUCT = "vcf-logs"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_BUILTIN_TYPED_OP = "vrli.event.query"
_BUILTIN_READ_OP = "GET:/api/v2/events"
_LEGACY_GROUP_COUNT = 2
_LEGACY_OPS_PER_GROUP = 3


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_operator(*, tenant_id: uuid.UUID, platform_admin: bool = False) -> Operator:
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
    )


async def _seed_builtin_copy() -> None:
    """Built-in ``vrli`` copy: one enabled group, one enabled typed op, one disabled read."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="events",
                name="Events",
                when_to_use="Query log events.",
                review_status="enabled",
            )
        )
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=_BUILTIN_TYPED_OP,
                source_kind="typed",
                method=None,
                path=None,
                group_id=group_id,
                summary="Query events",
                is_enabled=True,
            )
        )
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=_BUILTIN_READ_OP,
                source_kind="ingested",
                method="GET",
                path="/api/v2/events",
                group_id=group_id,
                summary="List events",
                is_enabled=False,
            )
        )
        await session.commit()


async def _seed_tenant_copy(tenant_id: uuid.UUID, *, product: str = _LEGACY_PRODUCT) -> None:
    """Tenant copy stored under *product*: staged groups, every op disabled."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for g_index in range(_LEGACY_GROUP_COUNT):
            group_id = uuid.uuid4()
            group_key = f"group-{g_index}"
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    group_key=group_key,
                    name=f"Group {g_index}",
                    when_to_use=f"Use group {g_index}.",
                    review_status="staged",
                )
            )
            for o_index in range(_LEGACY_OPS_PER_GROUP):
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=product,
                        version=_VERSION,
                        impl_id=_IMPL_ID,
                        op_id=f"GET:/api/v1/{group_key}/{o_index}",
                        source_kind="ingested",
                        method="GET",
                        path=f"/api/v1/{group_key}/{o_index}",
                        group_id=group_id,
                        summary=f"Operation {o_index} in {group_key}",
                        is_enabled=False,
                    )
                )
        await session.commit()


async def _rows(
    tenant_id: uuid.UUID | None, product: str
) -> tuple[dict[str, str], dict[str, bool]]:
    """Return ``({group_key: review_status}, {op_id: is_enabled})`` for one copy."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_stmt = select(OperationGroup).where(
            OperationGroup.product == product,
            OperationGroup.version == _VERSION,
            OperationGroup.impl_id == _IMPL_ID,
        )
        op_stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == _VERSION,
            EndpointDescriptor.impl_id == _IMPL_ID,
        )
        if tenant_id is None:
            group_stmt = group_stmt.where(OperationGroup.tenant_id.is_(None))
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            group_stmt = group_stmt.where(OperationGroup.tenant_id == tenant_id)
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        group_rows = (await session.execute(group_stmt)).scalars()
        op_rows = (await session.execute(op_stmt)).scalars()
        groups = {g.group_key: g.review_status for g in group_rows}
        ops = {o.op_id: o.is_enabled for o in op_rows}
        return groups, ops


_BUILTIN_BEFORE = (
    {"events": "enabled"},
    {_BUILTIN_TYPED_OP: True, _BUILTIN_READ_OP: False},
)


async def _legacy_delete_audit_payload() -> dict[str, Any]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(AuditLog).where(AuditLog.path == "meho_connector_delete")
        rows = list((await session.execute(stmt)).scalars().all())
    assert len(rows) == 1
    return dict(rows[0].payload)


def _assert_names_old_copy(exc: LegacyTenantConnectorCopyError, tenant_id: uuid.UUID) -> None:
    assert exc.connector_id == _CONNECTOR_ID
    assert exc.legacy_product == _LEGACY_PRODUCT
    assert exc.product == _PRODUCT
    assert exc.tenant_id == tenant_id
    message = str(exc)
    assert _LEGACY_PRODUCT in message
    assert f"meho connector delete {_CONNECTOR_ID}" in message
    assert "prefer=legacy" in message
    assert "not yet ingested" not in message
    assert "connector ingest" not in message
    assert exc.detail["detail"] == "connector_legacy_tenant_copy"


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_prefer_tenant_shows_old_copy() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="tenant")

    assert payload.connector_id == _CONNECTOR_ID
    assert payload.product == _LEGACY_PRODUCT
    assert payload.tenant_id == tenant_id
    assert [group.group_key for group in payload.groups] == ["group-0", "group-1"]
    assert payload.total_op_count == _LEGACY_GROUP_COUNT * _LEGACY_OPS_PER_GROUP
    assert payload.dispatchable is False


@pytest.mark.asyncio
async def test_review_without_prefer_refuses_and_names_old_copy() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    with pytest.raises(LegacyTenantConnectorCopyError) as caught:
        await service.get_review_payload(_CONNECTOR_ID, tenant_id)

    _assert_names_old_copy(caught.value, tenant_id)


@pytest.mark.asyncio
async def test_review_without_builtin_copy_still_names_old_copy_not_404() -> None:
    """Without a built-in row the answer is the same refusal, never a bare 404."""
    tenant_id = uuid.uuid4()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    with pytest.raises(LegacyTenantConnectorCopyError):
        await service.get_review_payload(_CONNECTOR_ID, tenant_id)
    payload = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="tenant")
    assert payload.product == _LEGACY_PRODUCT


@pytest.mark.asyncio
async def test_review_prefer_builtin_shows_builtin_copy() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="builtin")

    assert payload.product == _PRODUCT
    assert payload.tenant_id is None
    assert payload.total_op_count == 2


@pytest.mark.asyncio
async def test_review_prefer_legacy_shows_old_copy_next_to_current_copy() -> None:
    """With both tenant copies and the built-in copy, every copy has one selector.

    ``prefer=tenant`` reaches the current tenant copy, so without
    ``prefer=legacy`` the old copy, the one ``delete`` removes first,
    could not be looked at before it is removed.
    """
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    await _seed_tenant_copy(tenant_id, product=_PRODUCT)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    legacy = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="legacy")
    current = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="tenant")
    builtin = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="builtin")

    assert (legacy.product, legacy.tenant_id) == (_LEGACY_PRODUCT, tenant_id)
    assert legacy.total_op_count == _LEGACY_GROUP_COUNT * _LEGACY_OPS_PER_GROUP
    assert legacy.dispatchable is False
    assert (current.product, current.tenant_id) == (_PRODUCT, tenant_id)
    assert (builtin.product, builtin.tenant_id) == (_PRODUCT, None)
    # The current copy and the built-in copy still clash without a selector.
    with pytest.raises(AmbiguousConnectorScopeError):
        await service.get_review_payload(_CONNECTOR_ID, tenant_id)


@pytest.mark.asyncio
async def test_review_prefer_legacy_shows_old_copy_without_current_copy() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    payload = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="legacy")

    assert payload.product == _LEGACY_PRODUCT
    assert payload.tenant_id == tenant_id


@pytest.mark.asyncio
async def test_review_prefer_legacy_without_old_copy_is_not_found() -> None:
    """``prefer=legacy`` never slides to the current or the built-in copy."""
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id, product=_PRODUCT)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="legacy")
    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload(_CONNECTOR_ID, None, prefer="legacy")


# ---------------------------------------------------------------------------
# enable / disable / enable-reads / edits
# ---------------------------------------------------------------------------


async def _enable(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.enable_connector(_CONNECTOR_ID, tenant_id=tenant_id)


async def _disable(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.disable_connector(_CONNECTOR_ID, tenant_id=tenant_id)


async def _enable_reads(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.enable_reads(_CONNECTOR_ID, tenant_id=tenant_id)


async def _enable_reads_prefer_tenant(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.enable_reads(_CONNECTOR_ID, tenant_id=tenant_id, prefer="tenant")


async def _enable_reads_prefer_legacy(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.enable_reads(_CONNECTOR_ID, tenant_id=tenant_id, prefer="legacy")


async def _edit_group(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.edit_group(_CONNECTOR_ID, "group-0", tenant_id=tenant_id, name="Renamed")


async def _edit_op(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.edit_op(
        _CONNECTOR_ID, "GET:/api/v1/group-0/0", tenant_id=tenant_id, is_enabled=True
    )


async def _enable_group(service: ReviewService, tenant_id: uuid.UUID) -> None:
    await service.enable_group(_CONNECTOR_ID, "group-0", tenant_id=tenant_id)


_MUTATIONS = [
    _enable,
    _disable,
    _enable_reads,
    _enable_reads_prefer_tenant,
    _enable_reads_prefer_legacy,
    _edit_group,
    _edit_op,
    _enable_group,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_admin", [False, True], ids=["tenant_admin", "platform_admin"])
@pytest.mark.parametrize("mutate", _MUTATIONS, ids=[fn.__name__.strip("_") for fn in _MUTATIONS])
async def test_mutations_refuse_old_copy_and_leave_both_copies_unchanged(
    mutate: Any,
    platform_admin: bool,
) -> None:
    """No fall-back to the built-in row, no 403, no 404: a refusal that names the copy."""
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id, platform_admin=platform_admin))
    legacy_before = await _rows(tenant_id, _LEGACY_PRODUCT)

    with pytest.raises(LegacyTenantConnectorCopyError) as caught:
        await mutate(service, tenant_id)

    _assert_names_old_copy(caught.value, tenant_id)
    assert await _rows(None, _PRODUCT) == _BUILTIN_BEFORE
    assert await _rows(tenant_id, _LEGACY_PRODUCT) == legacy_before


@pytest.mark.asyncio
async def test_enable_reads_prefer_builtin_still_reaches_builtin_copy() -> None:
    """An explicit ``prefer=builtin`` is a choice, not a fall-back, so it still works."""
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id, platform_admin=True))

    flipped = await service.enable_reads(_CONNECTOR_ID, tenant_id=tenant_id, prefer="builtin")

    assert flipped == 1
    _groups, ops = await _rows(None, _PRODUCT)
    assert ops == {_BUILTIN_TYPED_OP: True, _BUILTIN_READ_OP: True}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_only_old_copy_and_builtin_op_still_dispatches() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    result = await service.delete_connector(_CONNECTOR_ID, tenant_id=tenant_id)

    assert result.groups_deleted == _LEGACY_GROUP_COUNT
    assert result.operations_deleted == _LEGACY_GROUP_COUNT * _LEGACY_OPS_PER_GROUP
    assert result.registry_only is False
    assert result.class_deregistered is False
    assert await _rows(tenant_id, _LEGACY_PRODUCT) == ({}, {})
    assert await _rows(None, _PRODUCT) == _BUILTIN_BEFORE

    payload = await _legacy_delete_audit_payload()
    assert payload["connector_id"] == _CONNECTOR_ID
    assert payload["tenant_scope"] == str(tenant_id)
    assert payload["product"] == _LEGACY_PRODUCT

    # Dispatch for this tenant still resolves the built-in typed op.
    row = await lookup_descriptor(
        tenant_id=tenant_id,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_BUILTIN_TYPED_OP,
    )
    assert row is not None
    assert row.tenant_id is None

    # With the old copy gone, the plain review falls back to the built-in row.
    review = await service.get_review_payload(_CONNECTOR_ID, tenant_id)
    assert review.product == _PRODUCT
    assert review.tenant_id is None


@pytest.mark.asyncio
async def test_delete_with_both_tenant_copies_removes_old_copy_first() -> None:
    """The listing's delete hint must never take away the tenant's current copy."""
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    await _seed_tenant_copy(tenant_id, product=_PRODUCT)
    current_before = await _rows(tenant_id, _PRODUCT)
    assert len(current_before[0]) == _LEGACY_GROUP_COUNT
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    # The operator can look at the copy the delete removes before removing it.
    to_delete = await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="legacy")
    assert to_delete.product == _LEGACY_PRODUCT

    result = await service.delete_connector(_CONNECTOR_ID, tenant_id=tenant_id)

    assert result.groups_deleted == _LEGACY_GROUP_COUNT
    assert await _rows(tenant_id, _LEGACY_PRODUCT) == ({}, {})
    assert await _rows(tenant_id, _PRODUCT) == current_before
    assert await _rows(None, _PRODUCT) == _BUILTIN_BEFORE
    payload = await _legacy_delete_audit_payload()
    assert payload["product"] == _LEGACY_PRODUCT

    # The old copy is gone, so there is nothing left for prefer=legacy to show.
    with pytest.raises(ConnectorNotFoundError):
        await service.get_review_payload(_CONNECTOR_ID, tenant_id, prefer="legacy")

    # The second delete removes the current tenant copy; the built-in copy stays.
    await service.delete_connector(_CONNECTOR_ID, tenant_id=tenant_id)
    assert await _rows(tenant_id, _PRODUCT) == ({}, {})
    assert await _rows(None, _PRODUCT) == _BUILTIN_BEFORE


@pytest.mark.asyncio
async def test_delete_from_another_tenant_leaves_old_copy_alone() -> None:
    owner = uuid.uuid4()
    other = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(owner)
    service = ReviewService(_make_operator(tenant_id=other))

    with pytest.raises(ConnectorNotFoundError):
        await service.delete_connector(_CONNECTOR_ID, tenant_id=other)

    groups, ops = await _rows(owner, _LEGACY_PRODUCT)
    assert len(groups) == _LEGACY_GROUP_COUNT
    assert len(ops) == _LEGACY_GROUP_COUNT * _LEGACY_OPS_PER_GROUP


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_does_not_shadow_builtin_and_marks_old_copy_uncallable() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)

    items = await list_ingested_connectors(operator=_make_operator(tenant_id=tenant_id))

    rows = {item.scope: item for item in items if item.connector_id == _CONNECTOR_ID}
    assert set(rows) == {"builtin", "tenant"}
    assert rows["builtin"].product == _PRODUCT
    assert rows["builtin"].shadowed_by_tenant_scope is False
    assert rows["builtin"].next_step is None
    legacy = rows["tenant"]
    assert legacy.product == _LEGACY_PRODUCT
    assert legacy.dispatchable is False
    assert legacy.shadowed_by_tenant_scope is False
    assert legacy.next_step is not None
    assert legacy.next_step.verb == f"meho connector delete {_CONNECTOR_ID}"
    assert _LEGACY_PRODUCT in legacy.next_step.rationale
    assert "never be called" in legacy.next_step.rationale


@pytest.mark.asyncio
async def test_list_with_only_old_copy_still_shows_it_with_delete_hint() -> None:
    """No built-in and no current-product row: the old copy must not be filtered out."""
    tenant_id = uuid.uuid4()
    await _seed_tenant_copy(tenant_id)

    items = await list_ingested_connectors(operator=_make_operator(tenant_id=tenant_id))

    tenant_rows = [
        item for item in items if item.connector_id == _CONNECTOR_ID and item.scope == "tenant"
    ]
    assert len(tenant_rows) == 1
    legacy = tenant_rows[0]
    assert legacy.product == _LEGACY_PRODUCT
    assert legacy.tenant_id == tenant_id
    assert legacy.state == "ingested"
    assert legacy.dispatchable is False
    assert legacy.next_step is not None
    assert legacy.next_step.verb == f"meho connector delete {_CONNECTOR_ID}"


@pytest.mark.asyncio
async def test_list_with_both_tenant_copies_hints_old_copy_and_shadows_builtin() -> None:
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id)
    await _seed_tenant_copy(tenant_id, product=_PRODUCT)

    items = await list_ingested_connectors(operator=_make_operator(tenant_id=tenant_id))

    rows = [item for item in items if item.connector_id == _CONNECTOR_ID]
    by_scope_product = {(item.scope, item.product): item for item in rows}
    assert set(by_scope_product) == {
        ("builtin", _PRODUCT),
        ("tenant", _PRODUCT),
        ("tenant", _LEGACY_PRODUCT),
    }
    assert by_scope_product[("builtin", _PRODUCT)].shadowed_by_tenant_scope is True
    current = by_scope_product[("tenant", _PRODUCT)]
    assert current.next_step is None
    legacy = by_scope_product[("tenant", _LEGACY_PRODUCT)]
    assert legacy.dispatchable is False
    assert legacy.next_step is not None
    assert legacy.next_step.verb == f"meho connector delete {_CONNECTOR_ID}"
    assert "old copy first" in legacy.next_step.rationale


@pytest.mark.asyncio
async def test_list_same_product_tenant_copy_still_shadows_builtin() -> None:
    """The #2474 marker keeps working for a real twin (same product)."""
    tenant_id = uuid.uuid4()
    await _seed_builtin_copy()
    await _seed_tenant_copy(tenant_id, product=_PRODUCT)

    items = await list_ingested_connectors(operator=_make_operator(tenant_id=tenant_id))

    rows = {item.scope: item for item in items if item.connector_id == _CONNECTOR_ID}
    assert rows["builtin"].shadowed_by_tenant_scope is True
    assert rows["tenant"].next_step is None
