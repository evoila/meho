# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run approval lists follow the rule of the people behind the login that asked.

An add-on's service login asks for a list of operations for one run. MEHO
looks up the people behind the login when a person approves the list: who
paired or owns the add-on, and who gave one of its standing grants in force.
MEHO finds the pairing by the login id, and by the client id that MEHO
stored on the list when the add-on asked.

* A person behind it may approve the list only when they are a person who
  may approve their own change.
* A list with a change of rights is refused for every admin.
* When MEHO knows nobody behind the login, only an Admin may approve.
* The lookup runs at approve time, so a list that waited across an upgrade
  follows the rule too.
"""

from __future__ import annotations

import uuid

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing
from meho_backplane.operations.grant_approval import _GRANT_OPS, _GrantOp
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import RunApprovalGroupError, RunApprovalService
from tests._people_behind_helpers import give_service_a_known_person
from tests.test_run_approvals import (
    _CONNECTOR,
    _PRINCIPAL,
    _RUN,
    _TENANT_A,
    _WRITE_OP,
    _descriptors,  # noqa: F401 -- autouse fixture: the operations the lists name
    _operator,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

_GIVER = "grant-giver-person"


def _person(sub: str, *, role: TenantRole = TenantRole.OPERATOR, sa: bool = False) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        self_approver=sa,
    )


async def _ask(asker: Operator | None = None) -> uuid.UUID:
    read = await RunApprovalService().request(
        asker or _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)],
            reason="build one VM",
        ),
    )
    return read.id


async def _status(approval_id: uuid.UUID) -> str:
    return (await RunApprovalService().get(_TENANT_A, approval_id)).status


async def test_the_giver_of_a_standing_grant_cannot_approve_as_an_operator() -> None:
    await give_service_a_known_person(_TENANT_A, _PRINCIPAL, _GIVER)
    approval_id = await _ask()

    with pytest.raises(RunApprovalGroupError, match="own_change"):
        await RunApprovalService().approve(_person(_GIVER), approval_id)
    assert await _status(approval_id) == "pending"


@pytest.mark.parametrize(
    "giver",
    [_person(_GIVER, sa=True), _person(_GIVER, role=TenantRole.TENANT_ADMIN)],
    ids=["sa-operator", "admin"],
)
async def test_a_person_behind_who_may_approve_their_own_change_approves(giver: Operator) -> None:
    await give_service_a_known_person(_TENANT_A, _PRINCIPAL, _GIVER)
    approval_id = await _ask()

    assert (await RunApprovalService().approve(giver, approval_id)).status == "approved"


async def test_the_pairing_owner_counts_too() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AddonPairing(
                tenant_id=_TENANT_A,
                name="builder",
                keycloak_client_id="addon:builder",
                keycloak_internal_id="kc-builder",
                service_account_sub=_PRINCIPAL,
                owner_sub="pairing-owner",
                contract_version=1,
                addon_contract_version=1,
                addon_min_backplane_version=1,
                created_by_sub="pairing-owner",
            )
        )
        await session.commit()
    approval_id = await _ask()

    with pytest.raises(RunApprovalGroupError, match="own_change"):
        await RunApprovalService().approve(_person("pairing-owner"), approval_id)
    assert (await RunApprovalService().approve(_person("someone-else"), approval_id)).status == (
        "approved"
    )


async def test_a_pairing_without_a_service_account_id_is_found_by_the_client_id() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AddonPairing(
                tenant_id=_TENANT_A,
                name="builder",
                keycloak_client_id="addon:builder",
                keycloak_internal_id="kc-builder",
                service_account_sub=None,
                owner_sub="pairing-owner",
                contract_version=1,
                addon_contract_version=1,
                addon_min_backplane_version=1,
                created_by_sub="pairing-owner",
            )
        )
        await session.commit()
    asker = _operator().model_copy(update={"client_id": "addon:builder"})
    approval_id = await _ask(asker)

    with pytest.raises(RunApprovalGroupError, match="own_change"):
        await RunApprovalService().approve(_person("pairing-owner"), approval_id)
    assert (await RunApprovalService().approve(_person("someone-else"), approval_id)).status == (
        "approved"
    )


async def test_only_an_admin_approves_when_nobody_is_known_behind_the_login() -> None:
    approval_id = await _ask()

    with pytest.raises(RunApprovalGroupError, match="requester_not_known"):
        await RunApprovalService().approve(_person("someone", sa=True), approval_id)
    admin = _person("an-admin", role=TenantRole.TENANT_ADMIN)
    assert (await RunApprovalService().approve(admin, approval_id)).status == "approved"


async def test_a_rights_change_list_is_refused_for_every_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run approval list never covers a change of rights: a person approves each call."""
    monkeypatch.setitem(_GRANT_OPS, _WRITE_OP, _GrantOp((), ()))
    await give_service_a_known_person(_TENANT_A, _PRINCIPAL, _GIVER)
    approval_id = await _ask()

    with pytest.raises(RunApprovalGroupError, match="rights_change_required"):
        await RunApprovalService().approve(
            _person(_GIVER, role=TenantRole.TENANT_ADMIN), approval_id
        )
    with pytest.raises(RunApprovalGroupError, match="rights_change_required"):
        await RunApprovalService().approve(
            _person("other-admin", role=TenantRole.TENANT_ADMIN), approval_id
        )


async def test_a_list_that_waited_before_the_grant_is_checked_at_approve_time() -> None:
    approval_id = await _ask()
    await give_service_a_known_person(_TENANT_A, _PRINCIPAL, _GIVER)

    with pytest.raises(RunApprovalGroupError, match="own_change"):
        await RunApprovalService().approve(_person(_GIVER), approval_id)
    assert (await RunApprovalService().approve(_person("someone-else"), approval_id)).status == (
        "approved"
    )
