# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The install's approver routing is a floor (``policy/routing_floor.py``).

* Import of a tenant's file: an entry narrower than the install's is
  accepted; a wider entry, another group, an added group and an entry that
  takes the routing away are refused, with a sentence that names the
  category and the install's groups.
* A category that the install does not route is free.
* A wider tenant entry that is already stored does not let a person
  approve, and an install file that changed later counts at the click.
* Two tenants do not affect each other; the install's import diff names
  the tenant whose routing no longer fits.
* A request that started to wait before MEHO stored its category is
  refused when an install entry that matches on a category may fit it.
* ``meho policy explain --request`` uses the install's routing as it is
  now, as the click does.
* With the switch off, nothing of this runs.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.operations.approval_queue import ApprovalRoutingError
from meho_backplane.policy.explain import _request_approvers
from meho_backplane.policy.routing import RouteEntry, approver_set_for
from meho_backplane.policy.routing_floor import (
    install_floor_refusal,
    may_share,
    tenant_floor_mistakes,
    wider_entries,
)
from meho_backplane.policy.service import build_plan
from meho_backplane.policy.validate import check_document
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_group_rules import _OTHER, _person
from tests.test_policy_routing import (
    _NETWORK_ONLY_SA,
    _approve,
    _load,
    _park,
    _routing_off,
    _routing_on,  # noqa: F401 -- fixture: turns POLICY_APPROVER_ROUTING on
    _store_policy,
)

_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_ALL_APPROVING = ["Operators", "SA-Operators", "Admins", "Approvers"]


def _tenant_file(*routes: dict[str, Any]) -> dict[str, Any]:
    return {"classifications": [], "routing": list(routes)}


def _import_mistakes(*routes: dict[str, Any]) -> list[str]:
    return check_document(
        _tenant_file(*routes), scope="tenant", install_document=_NETWORK_ONLY_SA, ops=[]
    )


def _sentence(place: str, extra: str) -> str:
    return (
        f"`{place}` lets {extra} approve changes with category `network`, but the install's "
        "routing (`install:routing[0]`) lets only MEHO-SA-Operators or MEHO-Admins approve "
        "them. A tenant can only narrow the install's routing: name only these groups, "
        "or fewer."
    )


# ---------------------------------------------------------------------------
# Import of a tenant's file
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_routing_on")
def test_a_narrower_entry_is_accepted() -> None:
    assert _import_mistakes({"match": {"category": "network"}, "approvers": ["Admins"]}) == []
    same = {"match": {"category": "network"}, "approvers": ["SA-Operators", "Admins"]}
    assert _import_mistakes(same) == []


@pytest.mark.usefixtures("_routing_on")
def test_a_wider_entry_and_an_added_group_are_refused() -> None:
    wider = {"match": {"category": "network"}, "approvers": ["Operators", "SA-Operators", "Admins"]}

    assert _import_mistakes(wider) == [_sentence("routing[0]", "MEHO-Operators")]


@pytest.mark.usefixtures("_routing_on")
def test_another_group_is_refused() -> None:
    other = {"match": {"category": "network", "risk": "caution"}, "approvers": ["Approvers"]}

    assert _import_mistakes(other) == [_sentence("routing[0]", "MEHO-Approvers")]


@pytest.mark.usefixtures("_routing_on")
@pytest.mark.parametrize(
    "match",
    [{"risk": "caution"}, {"connector": "*"}, {"connector": "nsx-4"}],
    ids=["by-risk", "every-connector", "one-connector"],
)
def test_an_entry_that_takes_the_routing_away_is_refused(match: dict[str, str]) -> None:
    mistakes = _import_mistakes(
        {"match": {"category": "storage"}, "approvers": ["Admins"]},
        {"match": match, "approvers": _ALL_APPROVING},
    )

    assert mistakes == [_sentence("routing[1]", "MEHO-Operators or MEHO-Approvers")]


@pytest.mark.usefixtures("_routing_on")
def test_a_category_the_install_does_not_route_is_free() -> None:
    free = {"match": {"category": "storage"}, "approvers": _ALL_APPROVING}

    assert _import_mistakes(free) == []


@pytest.mark.usefixtures("_routing_on")
def test_no_install_routing_leaves_the_tenant_free() -> None:
    document = _tenant_file({"match": {"category": "network"}, "approvers": _ALL_APPROVING})

    assert tenant_floor_mistakes(document, None) == []
    assert tenant_floor_mistakes(document, {"classifications": []}) == []


def test_with_the_switch_off_the_import_does_not_check_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _routing_off(monkeypatch)
    wider = {"match": {"category": "network"}, "approvers": _ALL_APPROVING}

    assert _import_mistakes(wider) == []


def _install_entry(**match: str) -> RouteEntry:
    return RouteEntry(ref="install:routing[0]", approvers=("Admins",), install=True, **match)


def _tenant_entry(**match: str) -> RouteEntry:
    return RouteEntry(ref="tenant:routing[0]", approvers=("Operators",), **match)


@pytest.mark.parametrize(
    ("tenant", "install", "shared"),
    [
        ({"category": "storage"}, {"category": "network"}, False),
        ({"risk": "safe"}, {"risk": "caution"}, False),
        ({"category": "network"}, {"risk": "caution"}, True),
        ({"connector": "vcenter-8"}, {"connector": "nsx-*"}, False),
        ({"connector": "nsx-4"}, {"connector": "nsx-*"}, True),
        ({"connector": "nsx-*"}, {"connector": "nsx-4"}, True),
        ({"connector": "nsx-*"}, {"connector": "nsx-4.?"}, True),
        ({"connector": "nsx-*"}, {"connector": "nsx-*"}, True),
        ({}, {"connector": "nsx-*"}, True),
    ],
)
def test_which_entries_can_share_a_change(
    tenant: dict[str, str], install: dict[str, str], shared: bool
) -> None:
    assert may_share(_tenant_entry(**tenant), _install_entry(**install)) is shared


def test_a_tenant_entry_that_shares_no_change_is_not_checked() -> None:
    install = [_install_entry(connector="nsx-*")]

    assert wider_entries([_tenant_entry(connector="vcenter-8")], install) == []
    assert len(wider_entries([_tenant_entry(connector="nsx-4")], install)) == 1


# ---------------------------------------------------------------------------
# The run and the click
# ---------------------------------------------------------------------------


_WIDER_TENANT = _tenant_file(
    {"match": {"category": "network"}, "approvers": ["Operators", "SA-Operators", "Admins"]}
)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_stored_wider_entry_does_not_let_a_person_approve() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    # Stored before the rule existed: the import would refuse it now.
    await _store_policy(_WIDER_TENANT, tenant_id=_TENANT_A)

    request_id = await _park("caution")
    row = await _load(request_id)

    assert row.approver_roles == ["SA-Operators", "Admins"]
    assert row.approver_rule == "install:routing[0], tenant:routing[0]"
    with pytest.raises(ApprovalRoutingError) as caught:
        await _approve(request_id, _person(_OTHER))
    assert "lets only MEHO-SA-Operators or MEHO-Admins approve" in caught.value.reason_text
    assert (await _approve(request_id, _person(_OTHER, sa_operator=True))).status == "approved"


async def _set_install(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        ).scalar_one()
        row.document = document
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_an_install_file_that_changed_later_counts_at_the_click() -> None:
    await _store_policy(
        {
            "classifications": [],
            "routing": [{"match": {"category": "network"}, "approvers": _ALL_APPROVING}],
        }
    )
    request_id = await _park("caution")
    assert "Operators" in (await _load(request_id)).approver_roles

    await _set_install(_NETWORK_ONLY_SA)

    with pytest.raises(ApprovalRoutingError) as caught:
        await _approve(request_id, _person(_OTHER))
    assert caught.value.reason_text == (
        "The install's routing (`install:routing[0]`) lets only MEHO-SA-Operators or "
        "MEHO-Admins approve this change. You are in MEHO-Operators. A tenant's routing can "
        "only narrow it."
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_stored_install_routing_that_is_not_valid_refuses_the_click() -> None:
    await _store_policy({"classifications": []})
    request_id = await _park("caution")
    await _set_install({"classifications": [], "routing": "not a list"})

    with pytest.raises(ApprovalRoutingError) as caught:
        await _approve(request_id, _person(_OTHER, sa_operator=True))
    assert caught.value.reason_text.startswith("The install's stored routing is not valid")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_rights_change_and_an_unrouted_category_pass_the_floor() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    rights = await _load(await _park("caution", rights_change=True))
    storage = await _load(await _park("caution", category="storage"))

    async with get_sessionmaker()() as session:
        for row in (rights, storage):
            assert await install_floor_refusal(session, _person(_OTHER), row) is None


@pytest.mark.asyncio
async def test_with_the_switch_off_the_click_does_not_check_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _routing_off(monkeypatch)
    await _store_policy(_NETWORK_ONLY_SA)
    row = await _load(await _park("caution"))

    assert row.approver_category is None
    async with get_sessionmaker()() as session:
        assert await install_floor_refusal(session, _person(_OTHER), row) is None


async def _from_before_the_category(request_id: uuid.UUID) -> None:
    """Make the row look like one that started to wait before migration ``0171``."""
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        row.approver_roles = list(_ALL_APPROVING)
        row.approver_rule = "tenant:routing[0]"
        row.approver_category = ""
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_request_from_before_the_category_is_refused_when_a_category_entry_may_fit() -> (
    None
):
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")
    await _from_before_the_category(request_id)

    for person in (_person(_OTHER), _person(_OTHER, sa_operator=True)):
        with pytest.raises(ApprovalRoutingError) as caught:
            await _approve(request_id, person)
        assert caught.value.reason_text.startswith(
            "This request started to wait before MEHO stored the category of a change"
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_request_from_before_the_category_is_routed_when_no_category_entry_fits() -> None:
    await _store_policy(
        {
            "classifications": [],
            "routing": [
                {"match": {"category": "network", "connector": "other-*"}, "approvers": ["Admins"]},
                {"match": {"risk": "caution"}, "approvers": ["SA-Operators", "Admins"]},
            ],
        }
    )
    request_id = await _park("caution")
    await _from_before_the_category(request_id)

    with pytest.raises(ApprovalRoutingError) as caught:
        await _approve(request_id, _person(_OTHER))
    assert "(`install:routing[1]`) lets only MEHO-SA-Operators" in caught.value.reason_text
    assert (await _approve(request_id, _person(_OTHER, sa_operator=True))).status == "approved"


# ---------------------------------------------------------------------------
# Two tenants, and the install's import diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_two_tenants_do_not_affect_each_other() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    await _store_policy(
        _tenant_file({"match": {"category": "network"}, "approvers": ["Admins"]}),
        tenant_id=_TENANT_A,
    )

    async with get_sessionmaker()() as session:
        found = {
            tenant: await approver_set_for(
                session,
                tenant_id=tenant,
                connector_id="demo-1.x",
                op_id="demo.change",
                level="caution",
                category="network",
            )
            for tenant in (_TENANT_A, _TENANT_B)
        }

    assert found[_TENANT_A].kinds == ("Admins",)
    assert found[_TENANT_B].kinds == ("SA-Operators", "Admins")
    assert found[_TENANT_B].rule == "install:routing[0]"
    assert found[_TENANT_B].floor == "install:routing[0]"


_NEW_INSTALL_FILE = b"policy: v1\nrouting:\n  - {match: {category: network}, approvers: [Admins]}\n"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_the_install_diff_names_the_tenants_whose_routing_no_longer_fits() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    await _store_policy(_tenant_file(*_NETWORK_ONLY_SA["routing"]), tenant_id=_TENANT_A)
    await _store_policy(
        _tenant_file({"match": {"category": "network"}, "approvers": ["Admins"]}),
        tenant_id=_TENANT_B,
    )

    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=_NEW_INSTALL_FILE)

    assert plan.mistakes == []
    assert plan.diff is not None
    sentence = (
        "`routing[0]` lets MEHO-SA-Operators approve changes with category `network`, but the "
        "install's routing (`install:routing[0]`) lets only MEHO-Admins approve them. A tenant "
        "can only narrow the install's routing: name only these groups, or fewer."
    )
    assert plan.diff["routing_floor"] == [{"tenant_id": str(_TENANT_A), "entries": [sentence]}]
    assert (
        f"Tenant {_TENANT_A}: {sentence} Where it is wider, the entry does not count."
        in plan.warnings
    )


@pytest.mark.asyncio
async def test_with_the_switch_off_the_install_diff_names_no_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _routing_off(monkeypatch)
    await _store_policy(_NETWORK_ONLY_SA)
    await _store_policy(_tenant_file(*_NETWORK_ONLY_SA["routing"]), tenant_id=_TENANT_A)

    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=_NEW_INSTALL_FILE)

    assert plan.diff is not None
    assert plan.diff["routing_floor"] == []


# ---------------------------------------------------------------------------
# meho policy explain
# ---------------------------------------------------------------------------


async def _explain(request_id: uuid.UUID) -> Any:
    row = await _load(request_id)
    async with get_sessionmaker()() as session:
        return await _request_approvers(session, row, "caution")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_explain_of_a_stored_request_names_the_installs_floor() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    await _store_policy(
        _tenant_file({"match": {"category": "network"}, "approvers": ["Admins"]}),
        tenant_id=_TENANT_A,
    )
    request_id = await _park("caution")

    found = await _explain(request_id)

    assert found.groups == ("MEHO-Admins",)
    assert found.rule == "install:routing[0], tenant:routing[0]"
    assert found.floor == "install:routing[0]"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_explain_of_a_stored_request_uses_the_install_routing_as_it_is_now() -> None:
    await _store_policy({"classifications": []})
    request_id = await _park("caution")
    before = await _explain(request_id)
    assert "MEHO-Operators" in before.groups
    assert before.floor is None

    # The install adds routing after the request started to wait.
    await _set_install(_NETWORK_ONLY_SA)
    added = await _explain(request_id)
    assert added.groups == ("MEHO-SA-Operators", "MEHO-Admins")
    assert added.floor == "install:routing[0]"

    # The install narrows its routing further.
    await _set_install(
        {
            "classifications": [],
            "routing": [{"match": {"category": "network"}, "approvers": ["Admins"]}],
        }
    )
    narrowed = await _explain(request_id)
    assert narrowed.groups == ("MEHO-Admins",)
    assert narrowed.floor == "install:routing[0]"
    with pytest.raises(ApprovalRoutingError):
        await _approve(request_id, _person(_OTHER, sa_operator=True))


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_explain_of_a_request_from_before_the_category_names_no_group() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")
    await _from_before_the_category(request_id)

    found = await _explain(request_id)

    assert found.groups == ()
    assert found.rule == "category_not_stored"
    assert found.floor is None
