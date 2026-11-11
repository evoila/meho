# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Classification "always wait" against standing grants and run approvals.

* An operation that a classification entry makes always wait (or that two
  entries give a conflict) is never cleared by a standing grant, also one
  made before the entry, and never by a run approval.
* ``policy.classify`` is approved call by call: no grant covers it.
* With the ``POLICY_CLASSIFICATION`` switch turned off after a start with
  it on, ``policy.classify`` is no longer found, listed or parked.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.classify import (
    POLICY_CLASSIFY_OP_ID,
    POLICY_REGISTRY_CONNECTOR_ID,
    register_policy_classify_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationGroup,
    PermissionVerdict,
    ServicePrincipalGrant,
    TenantPolicy,
)
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.meta_tools import (
    UnknownConnectorError,
    call_operation,
    list_operation_groups,
)
from meho_backplane.operations.run_approval_schemas import RunApprovalEntry
from meho_backplane.operations.run_approvals import _entry_refusal
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006271")
_FIX = {"product": "clsgrant", "version": "1.x", "impl_id": "clsgrant"}
_CONNECTOR = "clsgrant-1.x"
_OP = "clsgrant.vm.restart"
_SERVICE_SUB = "svc:classified"

_ALWAYS_WAIT = {"classifications": [{"match": {"group": "compute"}, "always_wait": True}]}
_CONFLICT = {
    "classifications": [
        {"match": {"op": "clsgrant.vm.*"}, "category": "compute"},
        {"match": {"op": "clsgrant.vm.?estart"}, "category": "storage"},
    ]
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if on else "false")
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(embedding: AsyncMock) -> None:
    """A ``caution`` op: a service call waits unless a grant clears it."""
    await register_typed_operation(
        **_FIX,
        op_id=_OP,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="compute",
        when_to_use="Fixture group.",
        safety_level="caution",
        embedding_service=embedding,
    )


async def _store_policy(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=None, document=document, set_by_sub="fixture"))
        await session.commit()


async def _seed_grant(op_id: str = _OP, connector_id: str = _CONNECTOR) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=connector_id,
                target_id=None,
                reason="fixture: made before the classification",
                created_by_sub="fixture-admin",
            )
        )
        await session.commit()


def _service() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


def _admin() -> Operator:
    return Operator(
        sub="admin-a",
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _gate() -> PermissionVerdict:
    descriptor = await lookup_descriptor(tenant_id=_TENANT, op_id=_OP, **_FIX)
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=_service(), descriptor=descriptor, target=None, connector_id=_CONNECTOR
    )
    return verdict


def _grant_payload(op_id: str = _OP, connector_id: str = _CONNECTOR) -> ServiceGrantCreate:
    return ServiceGrantCreate(
        principal_sub=_SERVICE_SUB,
        op_id=op_id,
        connector_id=connector_id,
        target_id=None,
        reason="fixture: try to grant a classified operation",
        expires_at=None,
    )


@pytest.mark.asyncio
async def test_existing_grant_clears_the_operation_while_the_switch_is_off(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case: the same grant and entry, but nothing reads the entry."""
    _switch(monkeypatch, False)
    await _register(embedding)
    await _store_policy(_ALWAYS_WAIT)
    await _seed_grant()
    assert await _gate() is PermissionVerdict.AUTO_EXECUTE


@pytest.mark.parametrize("document", [_ALWAYS_WAIT, _CONFLICT], ids=["always_wait", "conflict"])
@pytest.mark.asyncio
async def test_existing_grant_does_not_clear_an_always_wait_operation(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding)
    await _seed_grant()
    await _store_policy(document)

    descriptor = await lookup_descriptor(tenant_id=_TENANT, op_id=_OP, **_FIX)
    assert descriptor is not None
    assert (descriptor.requires_approval, descriptor.policy_always_wait) == (True, True)
    assert await _gate() is PermissionVerdict.NEEDS_APPROVAL


@pytest.mark.parametrize("document", [_ALWAYS_WAIT, _CONFLICT], ids=["always_wait", "conflict"])
@pytest.mark.asyncio
async def test_no_new_grant_and_no_run_approval_for_an_always_wait_operation(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]
) -> None:
    _switch(monkeypatch, True)
    await _register(embedding)
    await _store_policy(document)

    with pytest.raises(GrantValidationError, match="classified to always wait"):
        await ServicePrincipalGrantService().create(_TENANT, "fixture-admin", _grant_payload())
    refusal = await _entry_refusal(_TENANT, RunApprovalEntry(connector_id=_CONNECTOR, op_id=_OP))
    assert refusal is not None
    assert "classified to always wait" in refusal


@pytest.mark.asyncio
async def test_policy_classify_is_approved_call_by_call(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await register_policy_classify_operation(embedding_service=embedding)
    assert needs_per_call_approval(POLICY_CLASSIFY_OP_ID)

    with pytest.raises(GrantValidationError, match="a person approves each call"):
        await ServicePrincipalGrantService().create(
            _TENANT,
            "fixture-admin",
            _grant_payload(POLICY_CLASSIFY_OP_ID, POLICY_REGISTRY_CONNECTOR_ID),
        )
    refusal = await _entry_refusal(
        _TENANT,
        RunApprovalEntry(connector_id=POLICY_REGISTRY_CONNECTOR_ID, op_id=POLICY_CLASSIFY_OP_ID),
    )
    assert refusal is not None

    # A grant row stored some other way does not clear a call either.
    await _seed_grant(POLICY_CLASSIFY_OP_ID, POLICY_REGISTRY_CONNECTOR_ID)
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product="policy",
        version="1.x",
        impl_id="policy-registry",
        op_id=POLICY_CLASSIFY_OP_ID,
    )
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=_service(),
        descriptor=descriptor,
        target=None,
        connector_id=POLICY_REGISTRY_CONNECTOR_ID,
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_switch_on_then_off_removes_policy_classify(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start with the switch on, then one with it off, on the same database."""
    _switch(monkeypatch, True)
    await register_policy_classify_operation(embedding_service=embedding)
    groups = await list_operation_groups(_admin(), {"connector_id": POLICY_REGISTRY_CONNECTOR_ID})
    assert [g["group_key"] for g in groups["groups"]] == ["policy"]

    _switch(monkeypatch, False)
    await register_policy_classify_operation(embedding_service=embedding)

    async with get_sessionmaker()() as session:
        for model in (EndpointDescriptor, OperationGroup):
            rows = (await session.execute(select(model).where(model.product == "policy"))).all()
            assert rows == [], model
    with pytest.raises(UnknownConnectorError):
        await list_operation_groups(_admin(), {"connector_id": POLICY_REGISTRY_CONNECTOR_ID})
    call = await call_operation(
        _admin(),
        {
            "connector_id": POLICY_REGISTRY_CONNECTOR_ID,
            "op_id": POLICY_CLASSIFY_OP_ID,
            "params": {"match": {"connector": "vault-*"}, "category": "secrets"},
        },
    )
    assert call["status"] != "awaiting_approval", call
    assert call["status"] == "error", call
