# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing by category on a real call (``POLICY_APPROVER_ROUTING``).

The category is the one the classification gives the operation
(``POLICY_CLASSIFICATION``). Nothing here puts it on the request by hand:
the call goes through ``call_operation``, waits for approval, and the
request stores the group kinds of the ``category`` entry.

A tenant may move the operation to another category for its own rules:
by file import, by the single classification edit, by an override of the
operation, or by a row written straight into the database. The install's
routing still uses the category the install's file alone gives, so the
click and ``meho policy explain`` keep the install's groups.
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
from meho_backplane.db.models import ApprovalRequest, OperationPolicyOverride, TenantPolicy
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.approval_queue import ApprovalRoutingError, approve_request
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.document import file_sha256
from meho_backplane.policy.explain import _request_approvers
from meho_backplane.policy.service import apply_policy, build_plan
from meho_backplane.policy.store import ClassificationEdit, set_classification
from meho_backplane.policy.who_approves import approvers_for
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


@pytest.mark.asyncio
async def test_a_waiting_step_stores_the_kinds_of_its_category() -> None:
    """The step gate of a multi-step operation passes the step's category on."""
    await _store_policy(_NETWORK_TO_SA)

    gate = await enforce_subop_policy(
        operator=_service(),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=True,
        target=None,
        params={},
    )

    assert gate is not None
    assert gate.status == "awaiting_approval", gate
    row = await _request()
    assert row.approver_roles == ["SA-Operators", "Admins"]
    assert row.approver_rule == "install:routing[0]"


# ---------------------------------------------------------------------------
# A tenant that moves the operation to another category
# ---------------------------------------------------------------------------

_ALL = "[Operators, SA-Operators, Admins, Approvers]"
_INSTALL_FILE = (
    "policy: v1\n"
    "classifications:\n"
    "  - {match: {connector: 'clsroute-*'}, category: network}\n"
    "routing:\n"
    "  - {match: {category: network}, approvers: [Admins]}\n"
)
_MOVE = f"{{match: {{connector: {_CONNECTOR}, op: {_OP}}}, category: {{}}}}"


def _tenant_file(category: str, *, move: bool = True, own: bool = False) -> str:
    lines = ["policy: v1"]
    if own:
        lines.append(f"taxonomy: {{categories: [{category}]}}")
    if move:
        lines += ["classifications:", "  - " + _MOVE.replace("{}", category)]
    lines += ["routing:", f"  - {{match: {{category: {category}}}, approvers: {_ALL}}}"]
    return "\n".join(lines) + "\n"


async def _import(content: str, tenant_id: uuid.UUID | None = None) -> None:
    """Import *content* the way ``meho policy import`` does; the import must accept it."""
    scope = "install" if tenant_id is None else "tenant"
    raw = content.encode()
    async with get_sessionmaker()() as session, session.begin():
        plan = await build_plan(session, scope=scope, tenant_id=tenant_id, content=raw)
        assert plan.mistakes == []
        await apply_policy(
            session,
            scope=scope,
            tenant_id=tenant_id,
            content=raw,
            expected_sha256=file_sha256(raw),
            based_on=plan.based_on,
            imported_by="admin-a",
            approved_by="admin-b",
        )


async def _move_by_file_to_storage() -> None:
    await _import(_tenant_file("storage"), _TENANT)


async def _move_by_file_to_own_category() -> None:
    await _import(_tenant_file("mine", own=True), _TENANT)


async def _move_by_single_edit() -> None:
    await _import(_tenant_file("storage", move=False), _TENANT)
    edit = ClassificationEdit(
        match={"connector": _CONNECTOR, "op": _OP},
        category="storage",
        labels=None,
        always_wait=None,
    )
    async with get_sessionmaker()() as session, session.begin():
        await set_classification(session, tenant_id=_TENANT, set_by_sub="admin-a", edit=edit)


async def _move_by_override() -> None:
    await _import(_tenant_file("storage", move=False), _TENANT)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=_TENANT, op_id=_OP, category="storage", set_by_sub="admin-a", **_FIX
            )
        )


async def _move_by_database_row() -> None:
    document = {
        "classifications": [{"match": {"connector": _CONNECTOR, "op": _OP}, "category": "storage"}],
        "routing": [
            {
                "match": {"category": "storage"},
                "approvers": ["Operators", "SA-Operators", "Admins", "Approvers"],
            }
        ],
    }
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=_TENANT, document=document, set_by_sub="fixture"))
        await session.commit()


_MOVES = {
    "file-storage": (_move_by_file_to_storage, "storage"),
    "file-own-category": (_move_by_file_to_own_category, "mine"),
    "single-edit": (_move_by_single_edit, "storage"),
    "override": (_move_by_override, "storage"),
    "database-row": (_move_by_database_row, "storage"),
}


def _person(sub: str, role: TenantRole) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
    )


async def _approve(request_id: uuid.UUID, person: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=person, params={})
        await session.commit()
        return request


async def _explained(row: ApprovalRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The groups ``meho policy explain`` names for a call and for the waiting request."""
    descriptor = await lookup_descriptor(tenant_id=_TENANT, op_id=_OP, **_FIX)
    assert descriptor is not None
    call = await approvers_for(tenant_id=_TENANT, connector_id=_CONNECTOR, descriptor=descriptor)
    async with get_sessionmaker()() as session:
        request = await _request_approvers(session, row, "caution")
    return call.groups, request.groups


@pytest.mark.asyncio
@pytest.mark.parametrize("move", list(_MOVES))
async def test_a_tenant_that_moves_the_operation_keeps_the_install_routing(
    embedding: AsyncMock, move: str
) -> None:
    setup, category = _MOVES[move]
    await _register(embedding)
    await _import(_INSTALL_FILE)
    await setup()

    call = await call_operation(_service(), _CALL)

    assert call["status"] == "awaiting_approval", call
    row = await _request()
    assert row.approver_category == category
    assert row.approver_install_category == "network"
    assert row.approver_roles == ["Admins"]
    assert await _explained(row) == (("MEHO-Admins",), ("MEHO-Admins",))
    with pytest.raises(ApprovalRoutingError):
        await _approve(row.id, _person("person-operator", TenantRole.OPERATOR))
    approved = await _approve(row.id, _person("person-admin", TenantRole.TENANT_ADMIN))
    assert approved.status == "approved"


@pytest.mark.asyncio
async def test_the_click_routes_the_install_entries_with_the_install_category(
    embedding: AsyncMock,
) -> None:
    """Also when the stored groups are wider, the click keeps the install's groups."""
    await _register(embedding)
    await _import(_INSTALL_FILE)
    await _move_by_database_row()
    await call_operation(_service(), _CALL)
    row = await _request()
    async with get_sessionmaker()() as session:
        stored = await session.get(ApprovalRequest, row.id)
        assert stored is not None
        stored.approver_roles = ["Operators", "SA-Operators", "Admins", "Approvers"]
        await session.commit()

    assert await _explained(await _request()) == (("MEHO-Admins",), ("MEHO-Admins",))
    with pytest.raises(ApprovalRoutingError) as caught:
        await _approve(row.id, _person("person-operator", TenantRole.OPERATOR))
    assert caught.value.reason_text == (
        "The install's routing (`install:routing[0]`) lets only MEHO-Admins approve this "
        "change. You are in MEHO-Operators. A tenant's routing can only narrow it."
    )
