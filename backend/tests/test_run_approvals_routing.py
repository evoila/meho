# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run approvals use approver routing too (``POLICY_APPROVER_ROUTING``).

A run approval lists many operations. With the switch on, each entry is
routed like a single request, at the moment a person approves the list.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import RunApprovalGroupError, RunApprovalService
from meho_backplane.settings import get_settings
from tests.test_run_approvals import (
    _CONNECTOR,
    _RUN,
    _TENANT_A,
    _WRITE_OP,
    _approver,
    _descriptors,  # noqa: F401 -- autouse fixture: the operations the lists name
    _operator,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

#: Every caution change goes to the admins.
_CAUTION_TO_ADMINS: dict[str, Any] = {
    "classifications": [],
    "routing": [{"match": {"risk": "caution"}, "approvers": ["Admins"]}],
}


async def _store_policy(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=None, document=document, set_by_sub="fixture"))
        await session.commit()


async def _request_list() -> uuid.UUID:
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)],
            reason="one change",
        ),
    )
    return read.id


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true" if on else "false")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_person_outside_the_routed_kinds_cannot_approve_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, True)
    await _store_policy(_CAUTION_TO_ADMINS)
    approval_id = await _request_list()

    with pytest.raises(RunApprovalGroupError) as caught:
        await RunApprovalService().approve(_approver(role=TenantRole.OPERATOR), approval_id)

    assert str(caught.value) == (
        f"approval_group_required: entries[0] ({_WRITE_OP!r}): Only MEHO-Admins may approve "
        "this change (routing rule `install:routing[0]`). You are in MEHO-Operators."
    )
    assert (await RunApprovalService().get(_TENANT_A, approval_id)).status == "pending"


@pytest.mark.asyncio
async def test_a_person_in_the_routed_kinds_approves_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, True)
    await _store_policy(_CAUTION_TO_ADMINS)
    approval_id = await _request_list()

    read = await RunApprovalService().approve(_approver(role=TenantRole.TENANT_ADMIN), approval_id)

    assert read.status == "approved"


@pytest.mark.asyncio
async def test_with_the_switch_off_the_list_keeps_todays_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, False)
    await _store_policy(_CAUTION_TO_ADMINS)
    approval_id = await _request_list()

    read = await RunApprovalService().approve(_approver(role=TenantRole.OPERATOR), approval_id)

    assert read.status == "approved"


@pytest.mark.asyncio
async def test_a_category_entry_routes_a_classified_entry_of_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The category comes from the classification of the looked-up operation."""
    _switch(monkeypatch, True)
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    await _store_policy(
        {
            "classifications": [{"match": {"connector": _CONNECTOR}, "category": "network"}],
            "routing": [{"match": {"category": "network"}, "approvers": ["Admins"]}],
        }
    )
    approval_id = await _request_list()

    with pytest.raises(RunApprovalGroupError, match=r"routing rule `install:routing\[0\]`"):
        await RunApprovalService().approve(_approver(role=TenantRole.OPERATOR), approval_id)
    read = await RunApprovalService().approve(_approver(role=TenantRole.TENANT_ADMIN), approval_id)

    assert read.status == "approved"
