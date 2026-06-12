# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the connector DELETE surface (G0.25-T2 #1700).

Coverage matrix against the task's acceptance criteria, at the
service layer (:meth:`ReviewService.delete_connector` — the single
path both the REST route and the MCP tool drive):

* Row-bearing delete removes every ``operation_group`` +
  ``endpoint_descriptor`` row under the caller's scope (including
  ``group_id IS NULL`` strays the mid-pipeline-abort shape leaves),
  pops the triple's auto-shim from the v2 registry, writes exactly
  one ``meho.connector.delete`` audit row, and drops the connector
  from ``list_ingested_connectors``.
* Zero-op stub delete (the primary #1700 consumer scenario): no rows
  anywhere, only the auto-registered shim — registry-only delete.
* 404 conflation: unknown id, cross-tenant probe, built-in scope
  without tenant_admin, and rows that live only under a scope the
  caller did not name.
* Enabled-operations advisory: the delete completes, the result
  carries the ``enabled_operations_deleted`` warning.
* Registry policy: hand-coded classes are never deregistered; the
  shim survives while another tenant still has rows for the triple.
* Re-ingest revival: a fresh ``register_ingested_operations`` run on
  the deleted triple re-registers the shim and re-lands rows.
* Post-delete discovery: ``search_operations`` classifies the
  connector as unknown (no hits possible) once rows + shim are gone.

Runs against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` conftest fixture (schema pre-migrated to
head). Registry state is snapshot/restored per test by the conftest
global-registry isolation fixture (#585).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2, register_connector_v2
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import (
    ConnectorNotFoundError,
    ReviewService,
    ensure_connector_class_registered,
    list_ingested_connectors,
    register_ingested_operations,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.meta_tools import UnknownConnectorError, search_operations
from meho_backplane.settings import get_settings

_PRODUCT = "stubprobe"
_VERSION = "1.0"
_IMPL_ID = "stubprobe-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"


class _HandCodedConnector(Connector):
    """A non-shim stand-in for a hand-rolled per-product connector class.

    Local fake (same shape :mod:`tests.test_connectors_registry_v2`
    uses) rather than a real shipped class — importing a shipped
    connector package would fire its module-import-time registration
    side effects inside this test module.
    """

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1.0,<2.0"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction depends on.

    Same shape as :mod:`tests.test_operations_ingest_review` — the
    autouse conftest fixture pins ``DATABASE_URL``; Keycloak/Vault
    env come from the test file.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FAKE_JWT = "header.payload.signature"


def _make_operator(
    *,
    tenant_id: uuid.UUID,
    role: TenantRole = TenantRole.TENANT_ADMIN,
) -> Operator:
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=role,
    )


async def _seed_rows(
    *,
    tenant_id: uuid.UUID | None,
    group_count: int = 2,
    ops_per_group: int = 3,
    op_is_enabled: bool = False,
    stray_ungrouped_ops: int = 0,
) -> None:
    """Seed groups + child ops (and optional ``group_id IS NULL`` strays)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for g_index in range(group_count):
            group_id = uuid.uuid4()
            group_key = f"group-{g_index}"
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    group_key=group_key,
                    name=f"Group {g_index}",
                    when_to_use=f"Use group {g_index} for things.",
                    review_status="staged",
                ),
            )
            for o_index in range(ops_per_group):
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=_PRODUCT,
                        version=_VERSION,
                        impl_id=_IMPL_ID,
                        op_id=f"GET:/api/v1/{group_key}/{o_index}",
                        source_kind="ingested",
                        method="GET",
                        path=f"/api/v1/{group_key}/{o_index}",
                        summary="Summary",
                        description="Description",
                        group_id=group_id,
                        tags=["test"],
                        parameter_schema={"type": "object"},
                        safety_level="safe",
                        requires_approval=False,
                        is_enabled=op_is_enabled,
                    ),
                )
        for s_index in range(stray_ungrouped_ops):
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=f"GET:/api/v1/stray/{s_index}",
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/v1/stray/{s_index}",
                    summary="Stray",
                    description="Upserted by T2 but never grouped by T3.",
                    group_id=None,
                    tags=["test"],
                    parameter_schema={"type": "object"},
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=False,
                ),
            )
        await session.commit()


def _register_shim() -> None:
    assert ensure_connector_class_registered(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        base_url=None,
    )


async def _count_rows(tenant_id: uuid.UUID | None) -> tuple[int, int]:
    """Return ``(group_rows, descriptor_rows)`` for the test triple in scope."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_stmt = select(OperationGroup).where(
            OperationGroup.product == _PRODUCT,
            OperationGroup.version == _VERSION,
            OperationGroup.impl_id == _IMPL_ID,
        )
        op_stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == _PRODUCT,
            EndpointDescriptor.version == _VERSION,
            EndpointDescriptor.impl_id == _IMPL_ID,
        )
        if tenant_id is None:
            group_stmt = group_stmt.where(OperationGroup.tenant_id.is_(None))
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            group_stmt = group_stmt.where(OperationGroup.tenant_id == tenant_id)
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        groups = len((await session.execute(group_stmt)).scalars().all())
        ops = len((await session.execute(op_stmt)).scalars().all())
        return groups, ops


async def _delete_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.method == "SERVICE",
                AuditLog.path == "meho.connector.delete",
            ),
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Row-bearing delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_rows_shim_listing_and_audits() -> None:
    """Full delete: rows gone (incl. strays), shim gone, listing clean, one audit row."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    await _seed_rows(tenant_id=tenant, stray_ungrouped_ops=2)

    result = await ReviewService(operator).delete_connector(
        _CONNECTOR_ID,
        tenant_id=tenant,
    )

    assert result.groups_deleted == 2
    assert result.operations_deleted == 8  # 2 groups x 3 ops + 2 strays
    assert result.enabled_operations_deleted == 0
    assert result.warnings == ()
    assert result.class_deregistered is True
    assert result.registry_only is False
    assert await _count_rows(tenant) == (0, 0)
    assert (_PRODUCT, _VERSION, _IMPL_ID) not in all_connectors_v2()

    listed = await list_ingested_connectors(operator=operator)
    assert all(item.connector_id != _CONNECTOR_ID for item in listed)

    audit_rows = await _delete_audit_rows()
    assert len(audit_rows) == 1
    payload = audit_rows[0].payload
    assert payload["connector_id"] == _CONNECTOR_ID
    assert payload["tenant_scope"] == str(tenant)
    assert payload["deleted_group_keys"] == ["group-0", "group-1"]
    assert payload["groups_deleted"] == 2
    assert payload["operations_deleted"] == 8
    assert payload["class_deregistered"] is True
    assert payload["registry_only"] is False


@pytest.mark.asyncio
async def test_delete_warns_on_enabled_ops_but_completes() -> None:
    """A connector with enabled ops deletes anyway; the advisory names the count."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    await _seed_rows(tenant_id=tenant, op_is_enabled=True)

    result = await ReviewService(operator).delete_connector(
        _CONNECTOR_ID,
        tenant_id=tenant,
    )

    assert result.enabled_operations_deleted == 6
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert warning.code == "enabled_operations_deleted"
    assert warning.enabled_op_count == 6
    assert _CONNECTOR_ID in warning.message
    assert await _count_rows(tenant) == (0, 0)


@pytest.mark.asyncio
async def test_delete_preserves_other_scope_rows_and_shim() -> None:
    """Deleting one tenant's copy leaves the other scope's rows + the shim intact."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    await _seed_rows(tenant_id=tenant)
    await _seed_rows(tenant_id=None, group_count=1, ops_per_group=1)

    result = await ReviewService(operator).delete_connector(
        _CONNECTOR_ID,
        tenant_id=tenant,
    )

    assert result.class_deregistered is False
    assert await _count_rows(tenant) == (0, 0)
    assert await _count_rows(None) == (1, 1)
    assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()


@pytest.mark.asyncio
async def test_delete_never_deregisters_hand_coded_class() -> None:
    """Rows of a hand-coded connector delete; the class registration survives."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=_HandCodedConnector,
    )
    await _seed_rows(tenant_id=tenant)

    result = await ReviewService(operator).delete_connector(
        _CONNECTOR_ID,
        tenant_id=tenant,
    )

    assert result.class_deregistered is False
    assert await _count_rows(tenant) == (0, 0)
    assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()


# ---------------------------------------------------------------------------
# Zero-op stub (the primary #1700 scenario)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_zero_op_stub_is_registry_only() -> None:
    """No rows anywhere + registered shim: the delete pops the shim and audits."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()

    result = await ReviewService(operator).delete_connector(
        _CONNECTOR_ID,
        tenant_id=tenant,
    )

    assert result.registry_only is True
    assert result.class_deregistered is True
    assert result.groups_deleted == 0
    assert result.operations_deleted == 0
    assert result.warnings == ()
    assert (_PRODUCT, _VERSION, _IMPL_ID) not in all_connectors_v2()

    audit_rows = await _delete_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["registry_only"] is True

    listed = await list_ingested_connectors(operator=operator)
    assert all(item.connector_id != _CONNECTOR_ID for item in listed)


@pytest.mark.asyncio
async def test_delete_zero_op_stub_repeat_returns_not_found() -> None:
    """The second delete of a stub 404s — nothing is left to remove."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    service = ReviewService(operator)
    await service.delete_connector(_CONNECTOR_ID, tenant_id=tenant)

    with pytest.raises(ConnectorNotFoundError):
        await service.delete_connector(_CONNECTOR_ID, tenant_id=tenant)


# ---------------------------------------------------------------------------
# 404 conflation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_unknown_connector_raises_not_found() -> None:
    operator = _make_operator(tenant_id=uuid.uuid4())
    with pytest.raises(ConnectorNotFoundError):
        await ReviewService(operator).delete_connector(
            "missing-rest-9.9",
            tenant_id=operator.tenant_id,
        )


@pytest.mark.asyncio
async def test_delete_cross_tenant_scope_raises_not_found() -> None:
    """Naming another tenant's scope conflates into the same not-found error."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_a)
    _register_shim()
    await _seed_rows(tenant_id=tenant_b)

    with pytest.raises(ConnectorNotFoundError):
        await ReviewService(operator).delete_connector(
            _CONNECTOR_ID,
            tenant_id=tenant_b,
        )
    assert await _count_rows(tenant_b) == (2, 6)


@pytest.mark.asyncio
async def test_delete_rows_only_under_other_scope_raises_not_found() -> None:
    """Built-in rows are invisible to a tenant-scoped delete: 404, nothing changes.

    Pins the write-scope discipline the #1699 contract documents: the
    REST route always passes the operator's tenant, so a built-in
    connector must be deleted via the MCP tool's global scope — the
    tenant-scoped call cannot see (or take down) the NULL-scope rows,
    and the shim stays registered because rows still exist.
    """
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    await _seed_rows(tenant_id=None)

    with pytest.raises(ConnectorNotFoundError):
        await ReviewService(operator).delete_connector(
            _CONNECTOR_ID,
            tenant_id=tenant,
        )
    assert await _count_rows(None) == (2, 6)
    assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()


@pytest.mark.asyncio
async def test_delete_built_in_scope_requires_tenant_admin() -> None:
    """``tenant_id=None`` without tenant_admin conflates to not-found."""
    operator = _make_operator(tenant_id=uuid.uuid4(), role=TenantRole.OPERATOR)
    _register_shim()
    await _seed_rows(tenant_id=None)

    with pytest.raises(ConnectorNotFoundError):
        await ReviewService(operator).delete_connector(
            _CONNECTOR_ID,
            tenant_id=None,
        )
    assert await _count_rows(None) == (2, 6)


@pytest.mark.asyncio
async def test_delete_built_in_scope_with_tenant_admin_succeeds() -> None:
    """The MCP-shaped global-scope delete (tenant_id=None) removes built-in rows."""
    operator = _make_operator(tenant_id=uuid.uuid4())
    _register_shim()
    await _seed_rows(tenant_id=None)

    result = await ReviewService(operator).delete_connector(
        _CONNECTOR_ID,
        tenant_id=None,
    )

    assert result.class_deregistered is True
    assert await _count_rows(None) == (0, 0)
    audit_rows = await _delete_audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["tenant_scope"] is None


# ---------------------------------------------------------------------------
# Post-delete discovery + revival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_connector_is_unknown_to_search_operations() -> None:
    """Once rows + shim are gone, search classifies the connector as unknown.

    ``search_operations`` resolves the connector before touching the
    query embedding, so the post-delete probe raises
    :class:`UnknownConnectorError` without any hits — the strongest
    form of "deleted operations do not appear in search results".
    """
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    await _seed_rows(tenant_id=tenant)
    await ReviewService(operator).delete_connector(_CONNECTOR_ID, tenant_id=tenant)

    with pytest.raises(UnknownConnectorError):
        await search_operations(
            operator,
            {"connector_id": _CONNECTOR_ID, "query": "anything"},
        )


@pytest.mark.asyncio
async def test_reingest_after_delete_re_registers_connector() -> None:
    """AC: an ingest on a deleted connector re-registers it from scratch."""
    tenant = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant)
    _register_shim()
    await _seed_rows(tenant_id=tenant)
    await ReviewService(operator).delete_connector(_CONNECTOR_ID, tenant_id=tenant)
    assert (_PRODUCT, _VERSION, _IMPL_ID) not in all_connectors_v2()

    stub_embedding: Any = AsyncMock()
    stub_embedding.encode_one.return_value = [0.25] * 384
    stub_embedding.dimension = 384
    result = await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        operations=[
            EndpointDescriptorProto(
                op_id="GET:/pets",
                method="GET",
                path="/pets",
                summary="List pets",
                description="List the pets.",
                tags=["pets"],
                parameter_schema={"type": "object", "properties": {}},
                response_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
            ),
        ],
        spec_source="https://specs.example.test/reingest.yaml",
        tenant_id=tenant,
        embedding_service=stub_embedding,
    )

    assert result.connector_registered is True  # fresh shim, not a reuse
    assert result.inserted_count == 1
    assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()
    assert await _count_rows(tenant) == (0, 1)
