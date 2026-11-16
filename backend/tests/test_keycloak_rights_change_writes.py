# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Keycloak writes that are changes of rights.

``connectors/keycloak/rights_changes.py`` puts every Keycloak write on one
of two tables: a change of rights, or not one (with the reason). These
tests check:

* every registered Keycloak write is on exactly one table, so a new write
  fails here until it gets this decision;
* every change of rights is registered as one and carries the per-call
  mark, from the register function alone;
* for each change of rights: the requester cannot approve their own
  request, a MEHO-SA-Operator who is not an admin cannot approve it,
  another admin can, no standing grant can be made for it, an older grant
  row does not clear the wait, a run approval cannot list it or clear the
  wait, and an agent's call waits even with a grant that would run it.

Fixture values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from meho_backplane import op_classes
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import (  # loading the package registers the ops
    KeycloakConnector,
)
from meho_backplane.connectors.keycloak.ops_write_grant import GRANT_OPS
from meho_backplane.connectors.keycloak.rights_changes import (
    NOT_RIGHTS_CHANGE_OPS,
    RIGHTS_CHANGE_OPS,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    ApprovalRequestStatus,
    PermissionVerdict,
    RunApproval,
    ServicePrincipalGrant,
)
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations import grant_approval
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import compute_params_hash, policy_gate
from meho_backplane.operations.approval_queue import (
    RightsChangeApprovalError,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.grant_approval import (
    is_rights_change_op,
    register_grant_op,
    register_rights_change_op,
)
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

_CONNECTOR_ID = "keycloak-admin-26.x"
_RIGHTS = tuple(sorted(RIGHTS_CHANGE_OPS))
_RIGHTS_NOT_GRANT = tuple(op for op in _RIGHTS if op not in GRANT_OPS)
#: A ``caution`` write that is not a change of rights: a grant or a run
#: approval still covers it, so the tests show that the wait comes from
#: the mark and not from a grant that does not match.
_CONTROL = "keycloak.user.logout"
_PARAMS: dict[str, Any] = {"representation": {"name": "example"}}
_REQUESTER = "person-requester"
_SERVICE_SUB = "svc-keycloak-automation"
_RUN_REF = "run-keycloak-0001"


def _person(
    sub: str, *, role: TenantRole = TenantRole.TENANT_ADMIN, sa_operator: bool = False
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        self_approver=sa_operator,
    )


def _machine(kind: PrincipalKind, sub: str = _SERVICE_SUB) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _registered_writes(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Every Keycloak op the connector registers that is not a plain read."""
    seen: dict[str, tuple[str, bool]] = {}

    async def _capture(**kwargs: Any) -> None:
        seen[kwargs["op_id"]] = (kwargs["safety_level"], kwargs["requires_approval"])

    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.register_typed_operation", _capture
    )
    await KeycloakConnector.register_operations()
    assert seen, "the connector registered no operations"
    return {op for op, (level, approval) in seen.items() if level != "safe" or approval}


@pytest.mark.asyncio
async def test_every_keycloak_write_is_on_exactly_one_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = await _registered_writes(monkeypatch)

    assert not set(RIGHTS_CHANGE_OPS) & set(NOT_RIGHTS_CHANGE_OPS)
    missing = writes - set(RIGHTS_CHANGE_OPS) - set(NOT_RIGHTS_CHANGE_OPS)
    assert not missing, (
        f"Keycloak writes {sorted(missing)} are on neither table of "
        "connectors/keycloak/rights_changes.py. Add each one to RIGHTS_CHANGE_OPS "
        "(it can change what a token says or who has a role) or to "
        "NOT_RIGHTS_CHANGE_OPS with the reason."
    )
    stale = (set(RIGHTS_CHANGE_OPS) | set(NOT_RIGHTS_CHANGE_OPS)) - writes
    assert not stale, f"the tables name ops that are not Keycloak writes: {sorted(stale)}"
    assert all(RIGHTS_CHANGE_OPS.values()) and all(NOT_RIGHTS_CHANGE_OPS.values())


@pytest.mark.parametrize("op_id", _RIGHTS)
def test_each_change_of_rights_is_registered_with_the_per_call_mark(op_id: str) -> None:
    assert is_rights_change_op(op_id)
    assert needs_per_call_approval(op_id)


@pytest.mark.parametrize("op_id", sorted(NOT_RIGHTS_CHANGE_OPS))
def test_a_write_that_is_not_a_change_of_rights_is_not_registered(op_id: str) -> None:
    assert not is_rights_change_op(op_id)


@pytest.fixture
def _clean_registries() -> Iterator[None]:
    saved_per_call = set(op_classes._PER_CALL_APPROVAL)
    saved_rights = set(grant_approval._RIGHTS_CHANGE_OPS)
    saved_grants = dict(grant_approval._GRANT_OPS)
    yield
    op_classes._PER_CALL_APPROVAL.clear()
    op_classes._PER_CALL_APPROVAL.update(saved_per_call)
    grant_approval._RIGHTS_CHANGE_OPS.clear()
    grant_approval._RIGHTS_CHANGE_OPS.update(saved_rights)
    grant_approval._GRANT_OPS.clear()
    grant_approval._GRANT_OPS.update(saved_grants)


@pytest.mark.usefixtures("_clean_registries")
def test_registering_a_change_of_rights_adds_the_per_call_mark() -> None:
    register_rights_change_op("demo.rights.change")
    register_grant_op("demo.group.join", user_id_params=("user_id",), group_id_params=("g",))

    assert needs_per_call_approval("demo.rights.change")
    assert needs_per_call_approval("demo.group.join")


async def _park(op_id: str, requester: Operator) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"op_id": op_id, "safety_level": "dangerous"},
        )
        await session.commit()
        return request.id


async def _approve(request_id: uuid.UUID, operator: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=operator, params=_PARAMS)
        await session.commit()


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _RIGHTS)
async def test_the_requester_cannot_approve_their_own_request(op_id: str) -> None:
    requester = _person(_REQUESTER, sa_operator=True)
    request_id = await _park(op_id, requester)

    with pytest.raises(RightsChangeApprovalError):
        await _approve(request_id, requester)

    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _RIGHTS)
async def test_an_sa_operator_who_is_not_an_admin_cannot_approve(op_id: str) -> None:
    request_id = await _park(op_id, _person(_REQUESTER, role=TenantRole.OPERATOR))

    with pytest.raises(RightsChangeApprovalError):
        await _approve(request_id, _person("person-sa", role=TenantRole.OPERATOR, sa_operator=True))

    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _RIGHTS_NOT_GRANT)
async def test_another_admin_approves(op_id: str) -> None:
    request_id = await _park(op_id, _person(_REQUESTER, role=TenantRole.OPERATOR))

    await _approve(request_id, _person("person-admin"))

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _RIGHTS)
async def test_no_standing_grant_can_be_made(op_id: str) -> None:
    with pytest.raises(GrantValidationError, match="a person approves each call"):
        await ServicePrincipalGrantService().create(
            _TENANT_A,
            "person-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=_CONNECTOR_ID,
                reason="fixture: Keycloak automation",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _RIGHTS)
async def test_no_run_approval_can_list_it(op_id: str) -> None:
    with pytest.raises(RunApprovalValidationError, match="a person approves each call"):
        await RunApprovalService().request(
            _machine(PrincipalKind.SERVICE),
            RunApprovalCreate(
                work_ref=_RUN_REF,
                entries=[RunApprovalEntry(connector_id=_CONNECTOR_ID, op_id=op_id)],
                reason="fixture: Keycloak automation",
            ),
        )


async def _gate(operator: Operator, op_id: str) -> PermissionVerdict:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT_A,
        product="keycloak",
        version="26.x",
        impl_id="keycloak-admin",
        op_id=op_id,
    )
    assert descriptor is not None, op_id
    verdict, _reason = await policy_gate(
        operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR_ID
    )
    return verdict


async def _add_grant_rows(*op_ids: str) -> None:
    async with get_sessionmaker()() as session:
        for op_id in op_ids:
            session.add(
                ServicePrincipalGrant(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT_A,
                    principal_sub=_SERVICE_SUB,
                    op_id=op_id,
                    connector_id=_CONNECTOR_ID,
                    target_id=None,
                    reason="fixture: stored before the mark",
                    created_by_sub="person-admin",
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_an_older_grant_row_does_not_clear_the_wait() -> None:
    await KeycloakConnector.register_operations()
    # Written straight to the table, past the create check.
    await _add_grant_rows(*_RIGHTS, _CONTROL)
    service = _machine(PrincipalKind.SERVICE)

    waits = {op_id: await _gate(service, op_id) for op_id in _RIGHTS}
    assert waits == dict.fromkeys(_RIGHTS, PermissionVerdict.NEEDS_APPROVAL)
    assert await _gate(service, _CONTROL) is PermissionVerdict.AUTO_EXECUTE


@pytest.mark.asyncio
async def test_a_run_approval_does_not_clear_the_wait() -> None:
    await KeycloakConnector.register_operations()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT_A,
                principal_sub=_SERVICE_SUB,
                work_ref=_RUN_REF,
                entries=[
                    {"connector_id": _CONNECTOR_ID, "op_id": op_id}
                    for op_id in (*_RIGHTS, _CONTROL)
                ],
                reason="fixture: Keycloak automation",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-admin",
            )
        )
        await session.commit()
    service = _machine(PrincipalKind.SERVICE)

    token = work_ref_var.set(_RUN_REF)
    try:
        waits = {op_id: await _gate(service, op_id) for op_id in _RIGHTS}
        assert waits == dict.fromkeys(_RIGHTS, PermissionVerdict.NEEDS_APPROVAL)
        assert await _gate(service, _CONTROL) is PermissionVerdict.AUTO_EXECUTE
    finally:
        work_ref_var.reset(token)


@pytest.mark.asyncio
async def test_an_agent_call_waits_even_with_a_grant_that_runs_it() -> None:
    await KeycloakConnector.register_operations()
    agent = _machine(PrincipalKind.AGENT, sub="agent-keycloak")
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT_A,
                principal_sub=agent.sub,
                op_pattern="keycloak.*",
                verdict="auto-execute",
                created_by_sub="person-admin",
            )
        )
        await session.commit()

    waits = {op_id: await _gate(agent, op_id) for op_id in _RIGHTS}
    assert waits == dict.fromkeys(_RIGHTS, PermissionVerdict.NEEDS_APPROVAL)
