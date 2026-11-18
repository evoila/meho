# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The install's approver routing for steps, and the smaller points of the floor.

* A step of a multi-step operation reads the category of an override row
  of its operation, as a direct call does, so both get the same approvers.
* A request with stored group kinds but no stored install category: its
  category is not known while ``POLICY_CLASSIFICATION`` is on.
* Two install categories: nobody may approve, also when no routing entry
  names either category (the written rule).
* Import: a wider tenant entry that an earlier tenant entry hides, or whose
  connector pattern MEHO cannot compare, is a warning, not a refusal.
* ``meho policy export`` of a tenant marks a routing entry that is wider
  than the install's.
* The approval card reads the install's routing as it is now.
* The approve audit row names the install entry that counted.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.operations.approval_queue import ApprovalRoutingError
from meho_backplane.operations.approval_rights import ApproveRightReason, approve_right
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.policy.routing import CATEGORY_NOT_STORED, RouteEntry, route
from meho_backplane.policy.routing_floor import (
    InstallFloor,
    current_install_floor,
    tenant_floor_mistakes,
    tenant_floor_warnings,
)
from meho_backplane.policy.service import build_plan, export_policy
from meho_backplane.settings import get_settings
from tests.test_policy_routing_classified import (
    _CALL,
    _CONNECTOR,
    _FIX,
    _OP,
    _TENANT,
    _approve,
    _person,
    _register,
    _service,
    _settings_env,  # noqa: F401 -- autouse fixture: both policy switches on
    embedding,  # noqa: F401 -- fixture
)

_ALL = ["Operators", "SA-Operators", "Admins", "Approvers"]
_STORAGE_TO_ADMINS: dict[str, Any] = {
    "classifications": [],
    "routing": [{"match": {"category": "storage"}, "approvers": ["Admins"]}],
}
_NETWORK_TO_ADMINS: dict[str, Any] = {
    "classifications": [],
    "routing": [{"match": {"category": "network"}, "approvers": ["Admins"]}],
}


def _switch(monkeypatch: pytest.MonkeyPatch, name: str, on: bool) -> None:
    monkeypatch.setenv(name, "true" if on else "false")
    get_settings.cache_clear()


async def _store(document: dict[str, Any], tenant_id: uuid.UUID | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


async def _rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.op_id == _OP)
            .order_by(ApprovalRequest.created_at)
        )
        return list(result.scalars().all())


async def _direct_and_step() -> tuple[ApprovalRequest, ApprovalRequest]:
    """The same operation, once called and once as a step; both wait for approval."""
    call = await call_operation(_service(), _CALL)
    assert call["status"] == "awaiting_approval", call
    gate = await enforce_subop_policy(
        operator=_service(),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=False,
        target=None,
        params={},
    )
    assert gate is not None
    assert gate.status == "awaiting_approval", gate
    direct, step = await _rows()
    return direct, step


def _routing(row: ApprovalRequest) -> tuple[Any, ...]:
    return (
        row.approver_roles,
        row.approver_rule,
        row.approver_category,
        row.approver_install_category,
    )


# ---------------------------------------------------------------------------
# A step reads the category of an override row, like a direct call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("install", (["Admins"], "install:routing[0]", "storage", "storage")),
        ("tenant", (["Admins"], "tenant:routing[0]", "storage", "other")),
    ],
)
async def test_a_step_gets_the_same_approvers_as_a_direct_call(
    embedding: AsyncMock,  # noqa: F811 -- the fixture imported above
    scope: str,
    expected: tuple[Any, ...],
) -> None:
    await _register(embedding)
    await _store(_STORAGE_TO_ADMINS)
    tenant_id = None if scope == "install" else _TENANT
    if tenant_id is not None:
        await _store(_STORAGE_TO_ADMINS, tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=tenant_id, op_id=_OP, category="storage", set_by_sub="admin-a", **_FIX
            )
        )

    direct, step = await _direct_and_step()

    assert _routing(direct) == expected
    assert _routing(step) == expected
    for row in (direct, step):
        with pytest.raises(ApprovalRoutingError):
            await _approve(row.id, _person("person-operator", TenantRole.OPERATOR))
        approved = await _approve(row.id, _person("person-admin", TenantRole.TENANT_ADMIN))
        assert approved.status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("routing", "classification", "expected"),
    [
        (False, True, (None, None, None, None)),
        (True, False, (_ALL, "default", None, None)),
        (False, False, (None, None, None, None)),
    ],
    ids=["routing-off", "classification-off", "both-off"],
)
async def test_with_a_switch_off_the_override_changes_nothing_for_a_step(
    embedding: AsyncMock,  # noqa: F811 -- the fixture imported above
    monkeypatch: pytest.MonkeyPatch,
    routing: bool,
    classification: bool,
    expected: tuple[Any, ...],
) -> None:
    _switch(monkeypatch, "POLICY_APPROVER_ROUTING", routing)
    _switch(monkeypatch, "POLICY_CLASSIFICATION", classification)
    await _register(embedding)
    await _store(_STORAGE_TO_ADMINS)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=None, op_id=_OP, category="storage", set_by_sub="admin-a", **_FIX
            )
        )

    direct, step = await _direct_and_step()

    assert _routing(direct) == expected
    assert _routing(step) == expected


# ---------------------------------------------------------------------------
# Stored group kinds, no stored install category
# ---------------------------------------------------------------------------


def _waiting(*, install_category: str | None, kinds: list[str] | None = _ALL) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        principal_sub="svc:routed",
        op_id=_OP,
        connector_id=_CONNECTOR,
        params_hash="0" * 64,
        params={},
        proposed_effect={"op_id": _OP, "safety_level": "caution"},
        status="pending",
        approver_roles=kinds,
        approver_rule="tenant:routing[0]",
        approver_category="network",
        approver_install_category=install_category,
    )


_FLOOR = InstallFloor(
    entries=(
        RouteEntry(
            ref="install:routing[0]", approvers=("Admins",), category="network", install=True
        ),
    )
)


def test_no_stored_install_category_is_not_known_with_classification_on() -> None:
    row = _waiting(install_category=None)
    found = _FLOOR.check(row)

    assert found is not None
    assert found.rule == CATEGORY_NOT_STORED
    for role in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN):
        refusal = _FLOOR.refusal(_person("person", role), row)
        assert refusal is not None
        assert refusal.startswith("MEHO does not know the category")


def test_no_stored_install_category_fits_no_category_entry_with_classification_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the switch off no change has a category, as when the request started to wait."""
    _switch(monkeypatch, "POLICY_CLASSIFICATION", False)
    row = _waiting(install_category=None)

    found = _FLOOR.check(row)
    assert found is not None
    assert found.rule == "default"
    assert _FLOOR.refusal(_person("person-operator", TenantRole.OPERATOR), row) is None


def test_a_stored_install_category_is_used_as_stored() -> None:
    row = _waiting(install_category="network")

    operator = _person("person-operator", TenantRole.OPERATOR)
    assert _FLOOR.refusal(operator, row) == (
        "The install's routing (`install:routing[0]`) lets only MEHO-Admins approve this "
        "change. You are in MEHO-Operators. A tenant's routing can only narrow it."
    )
    assert _FLOOR.refusal(_person("person-admin", TenantRole.TENANT_ADMIN), row) is None


def test_a_request_without_stored_kinds_keeps_the_older_rule() -> None:
    row = _waiting(install_category=None, kinds=None)

    assert _FLOOR.check(row) is None
    assert _FLOOR.refusal(_person("person-operator", TenantRole.OPERATOR), row) is None


# ---------------------------------------------------------------------------
# Two install categories, no routing entry for either
# ---------------------------------------------------------------------------


def test_two_install_categories_let_nobody_approve_also_without_an_entry_for_them() -> None:
    """The written rule: MEHO cannot tell which category counts, so nobody approves."""
    entries = [
        RouteEntry(
            ref="install:routing[0]", approvers=("Admins",), category="compute", install=True
        )
    ]

    found = route(
        entries,
        category="mine",
        install_category="",
        connector_id=_CONNECTOR,
        level="caution",
        op_id=_OP,
    )

    assert found.kinds == ()
    assert found.rule == CATEGORY_NOT_STORED


# ---------------------------------------------------------------------------
# Import: warnings where the run takes the install's groups anyway
# ---------------------------------------------------------------------------


def _tenant(*routes: dict[str, Any]) -> dict[str, Any]:
    return {"classifications": [], "routing": list(routes)}


_SHADOWED = _tenant(
    {"match": {"category": "network"}, "approvers": ["Admins"]},
    {"match": {"risk": "caution"}, "approvers": _ALL},
)


def test_a_broad_entry_after_its_own_narrower_one_is_a_warning() -> None:
    assert tenant_floor_mistakes(_SHADOWED, _NETWORK_TO_ADMINS) == []
    assert tenant_floor_warnings(_SHADOWED, _NETWORK_TO_ADMINS) == [
        "`routing[1]` lets MEHO-Operators, MEHO-SA-Operators or MEHO-Approvers approve "
        "changes with category `network`, but the install's routing (`install:routing[0]`) "
        "lets only MEHO-Admins approve them. A tenant can only narrow the install's routing: "
        "name only these groups, or fewer. `routing[0]` comes first and fits each of these "
        "changes, so `routing[1]` does not count for them."
    ]


def test_an_earlier_entry_that_fits_only_some_changes_does_not_hide_a_wider_one() -> None:
    document = _tenant(
        {"match": {"category": "network", "risk": "safe"}, "approvers": ["Admins"]},
        {"match": {"risk": "caution"}, "approvers": _ALL},
        {"match": {"category": "storage"}, "approvers": ["Admins"]},
    )

    assert len(tenant_floor_mistakes(document, _NETWORK_TO_ADMINS)) == 1
    assert tenant_floor_warnings(document, _NETWORK_TO_ADMINS) == []


def test_two_different_connector_wildcards_are_a_warning() -> None:
    install = {
        "classifications": [],
        "routing": [{"match": {"connector": "nsx-*"}, "approvers": ["Admins"]}],
    }
    document = _tenant({"match": {"connector": "*-4.x"}, "approvers": _ALL})

    assert tenant_floor_mistakes(document, install) == []
    [warning] = tenant_floor_warnings(document, install)
    assert warning.endswith(
        "MEHO cannot tell whether the two connector patterns fit the same connector. Where "
        "they do, the entry does not count where it is wider."
    )


def test_a_literal_connector_inside_the_wildcard_is_still_refused() -> None:
    install = {
        "classifications": [],
        "routing": [{"match": {"connector": "nsx-*"}, "approvers": ["Admins"]}],
    }
    document = _tenant({"match": {"connector": "nsx-4.x"}, "approvers": _ALL})

    assert len(tenant_floor_mistakes(document, install)) == 1
    assert tenant_floor_warnings(document, install) == []


@pytest.mark.asyncio
async def test_the_import_plan_of_a_tenant_shows_the_warning() -> None:
    await _store(_NETWORK_TO_ADMINS)
    content = (
        b"policy: v1\n"
        b"routing:\n"
        b"  - {match: {category: network}, approvers: [Admins]}\n"
        b"  - {match: {risk: caution}, approvers: [Operators, SA-Operators, Admins, Approvers]}\n"
    )
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="tenant", tenant_id=_TENANT, content=content)

    assert plan.mistakes == []
    assert any("`routing[0]` comes first" in warning for warning in plan.warnings)


@pytest.mark.asyncio
async def test_the_install_diff_also_lists_a_tenant_the_import_only_warns_about() -> None:
    await _store(
        _tenant(
            {
                "match": {"connector": "*-4.x", "risk": "caution"},
                "approvers": ["Operators", "Admins"],
            }
        ),
        _TENANT,
    )
    content = (
        b"policy: v1\n"
        b"routing:\n"
        b"  - {match: {connector: nsx-*, risk: caution}, approvers: [Admins]}\n"
    )
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=content)

    assert plan.diff is not None
    [item] = plan.diff["routing_floor"]
    assert item["tenant_id"] == str(_TENANT)
    [entry] = item["entries"]
    assert entry.startswith("`routing[0]` lets MEHO-Operators approve changes")
    assert "MEHO cannot tell whether the two connector patterns" in entry
    assert any(warning.startswith(f"Tenant {_TENANT}: ") for warning in plan.warnings)


@pytest.mark.asyncio
async def test_the_install_diff_also_lists_an_entry_an_earlier_one_fits_first() -> None:
    await _store(_SHADOWED, _TENANT)
    content = b"policy: v1\nrouting:\n  - {match: {category: network}, approvers: [Admins]}\n"
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=content)

    assert plan.diff is not None
    [item] = plan.diff["routing_floor"]
    [entry] = item["entries"]
    assert "`routing[0]` comes first" in entry


def test_with_the_switch_off_the_import_has_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "POLICY_APPROVER_ROUTING", False)

    assert tenant_floor_warnings(_SHADOWED, _NETWORK_TO_ADMINS) == []


# ---------------------------------------------------------------------------
# meho policy export marks a wider routing entry
# ---------------------------------------------------------------------------

_WIDER = _tenant({"match": {"category": "network"}, "approvers": ["Operators", "Admins"]})


@pytest.mark.asyncio
async def test_the_export_marks_a_wider_routing_entry() -> None:
    await _store(_NETWORK_TO_ADMINS)
    await _store(_WIDER, _TENANT)

    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)

    assert (
        "routing:\n"
        "# Has no effect where wider: `routing[0]` lets MEHO-Operators approve changes with "
        "category `network`, but the install's routing (`install:routing[0]`) lets only "
        "MEHO-Admins approve them. A tenant can only narrow the install's routing: name only "
        "these groups, or fewer.\n"
        "- match:\n"
        "    category: network\n"
    ) in text


@pytest.mark.asyncio
async def test_with_the_switch_off_the_export_marks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "POLICY_APPROVER_ROUTING", False)
    await _store(_NETWORK_TO_ADMINS)
    await _store(_WIDER, _TENANT)

    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)

    assert "Has no effect" not in text
    assert "routing:\n- match:\n    category: network\n" in text


# ---------------------------------------------------------------------------
# The approval card, and the approve audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_card_reads_the_install_routing_as_it_is_now() -> None:
    await _store(_NETWORK_TO_ADMINS)
    row = _waiting(install_category="network")
    operator = _person("person-operator", TenantRole.OPERATOR)

    floor = await current_install_floor()
    right = approve_right(operator, row, floor=floor)

    assert right.may_approve is False
    assert right.reason == ApproveRightReason.NOT_ROUTED
    assert "lets only MEHO-Admins approve" in right.message
    admin = _person("person-admin", TenantRole.TENANT_ADMIN)
    assert approve_right(admin, row, floor=floor).may_approve is True


@pytest.mark.asyncio
async def test_with_the_switch_off_the_card_loads_no_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "POLICY_APPROVER_ROUTING", False)
    await _store(_NETWORK_TO_ADMINS)

    assert await current_install_floor() is None


@pytest.mark.asyncio
async def test_the_approve_audit_row_names_the_install_entry_that_counted(
    embedding: AsyncMock,  # noqa: F811 -- the fixture imported above
) -> None:
    await _register(embedding)
    await _store(_STORAGE_TO_ADMINS)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=None, op_id=_OP, category="storage", set_by_sub="admin-a", **_FIX
            )
        )
    call = await call_operation(_service(), _CALL)
    assert call["status"] == "awaiting_approval", call
    [row] = await _rows()

    await _approve(row.id, _person("person-admin", TenantRole.TENANT_ADMIN))

    async with get_sessionmaker()() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.path == "approval.decision",
                    AuditLog.payload["approval_request_id"].as_string() == str(row.id),
                )
            )
        ).scalar_one()
    assert audit.payload["approver_floor"] == "install:routing[0]"
