# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing by category on a real call (``POLICY_APPROVER_ROUTING``).

The category is the one the classification gives the operation
(``POLICY_CLASSIFICATION``). Nothing here puts it on the request by hand:
the call goes through ``call_operation``, waits for approval, and the
request stores the group kinds of the ``category`` entry.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006437")
_FIX = {"product": "clsroute", "version": "1.x", "impl_id": "clsroute"}
_CONNECTOR = "clsroute-1.x"
_OP = "clsroute.switch.port.set"

_NETWORK_TO_SA: dict[str, Any] = {
    "classifications": [{"match": {"connector": _CONNECTOR}, "category": "network"}],
    "routing": [{"match": {"category": "network"}, "approvers": ["SA-Operators", "Admins"]}],
}

_CALL: dict[str, Any] = {"connector_id": _CONNECTOR, "op_id": _OP, "params": {}}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()
    yield
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
    """A ``caution`` op: a service call waits for a person."""
    await register_typed_operation(
        **_FIX,
        op_id=_OP,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="switches",
        when_to_use="Fixture group.",
        safety_level="caution",
        embedding_service=embedding,
    )


async def _store_policy(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=None, document=document, set_by_sub="fixture"))
        await session.commit()


def _service() -> Operator:
    return Operator(
        sub="svc:routed",
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _request() -> ApprovalRequest:
    """The one request the call made."""
    async with get_sessionmaker()() as session:
        row: ApprovalRequest = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.op_id == _OP))
        ).scalar_one()
        return row


@pytest.mark.asyncio
async def test_the_looked_up_row_carries_its_category(embedding: AsyncMock) -> None:
    await _register(embedding)
    await _store_policy(_NETWORK_TO_SA)

    descriptor = await lookup_descriptor(tenant_id=_TENANT, op_id=_OP, **_FIX)

    assert descriptor is not None
    assert descriptor.policy_category == "network"


@pytest.mark.asyncio
async def test_a_waiting_call_stores_the_kinds_of_its_category(embedding: AsyncMock) -> None:
    await _register(embedding)
    await _store_policy(_NETWORK_TO_SA)

    call = await call_operation(_service(), _CALL)

    assert call["status"] == "awaiting_approval", call
    row = await _request()
    assert row.approver_roles == ["SA-Operators", "Admins"]
    assert row.approver_rule == "install:routing[0]"


@pytest.mark.asyncio
async def test_without_classification_a_category_entry_fits_no_call(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "false")
    get_settings.cache_clear()
    await _register(embedding)
    await _store_policy(_NETWORK_TO_SA)

    call = await call_operation(_service(), _CALL)

    assert call["status"] == "awaiting_approval", call
    row = await _request()
    assert row.approver_rule == "default"
