# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing (``POLICY_APPROVER_ROUTING``, ``policy/routing.py``).

* The ``routing`` section of the policy file: parse, import checks, diff,
  export.
* When a request starts to wait, it stores the group kinds that may approve
  it, from the routing entries. Never a person, so never the requester.
* At the click, a person outside the stored kinds gets one sentence that
  names who may approve. Today's checks still run.
* A request stored without kinds keeps today's rule. With the switch off,
  nothing is stored and nothing is checked.
* The shipped "separated duties" file is valid, and its two copies are the
  same.
"""

from __future__ import annotations

import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, TenantPolicy
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    ApprovalRoutingError,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.approval_rights import ApproveRightReason, approve_right
from meho_backplane.policy.classification import DEFAULT_TAXONOMY, OpCoordinates
from meho_backplane.policy.document import (
    GROUP_KINDS,
    canonical_sha256,
    document_of,
    parse_policy_file,
)
from meho_backplane.policy.routing import (
    DEFAULT_RULE,
    RouteEntry,
    kind_of,
    route,
    routes_from_documents,
    routing_mistakes,
    today_kinds,
    who_can_approve,
)
from meho_backplane.policy.service import _routing_diff, export_policy
from meho_backplane.policy.validate import KnownOp, check_document
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _client,  # noqa: F401 -- fixture: the REST test client and its signing key
    _fake_resume,  # noqa: F401 -- fixture: records the re-dispatch after approval
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_group_rules import _OTHER, _PARAMS, _REQUESTER, _person, _post, _rest_token

_NETWORK_ONLY_SA: dict[str, Any] = {
    "classifications": [],
    "routing": [
        {"match": {"category": "network"}, "approvers": ["SA-Operators", "Admins"]},
    ],
}


@pytest.fixture
def _routing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()


def _routing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "false")
    get_settings.cache_clear()


async def _store_policy(document: dict[str, Any], tenant_id: uuid.UUID | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


async def _park(
    level: str,
    *,
    category: str | None = "network",
    rights_change: bool = False,
    op_id: str = "demo.change",
) -> uuid.UUID:
    effect: dict[str, Any] = {"op_id": op_id, "safety_level": level}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_person(_REQUESTER),
            connector_id="demo-1.x",
            op_id=op_id,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect=effect,
            preview_hash="preview-hash" if level == "destructive" else None,
            rights_change=rights_change,
            category=category,
        )
        await session.commit()
        return request.id


async def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _approve(request_id: uuid.UUID, operator: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=operator, params=_PARAMS)
        await session.commit()
        return request


# ---------------------------------------------------------------------------
# The routing rules (no database)
# ---------------------------------------------------------------------------


def _entry(ref: str, approvers: tuple[str, ...], **match: str) -> RouteEntry:
    return RouteEntry(ref=ref, approvers=approvers, **match)


def test_a_category_entry_routes_its_changes_and_cuts_to_todays_kinds() -> None:
    entries = [_entry("install:routing[0]", ("Operators", "Admins"), category="network")]

    caution = route(
        entries, category="network", connector_id="demo-1.x", level="caution", op_id="demo.x"
    )
    dangerous = route(
        entries, category="network", connector_id="demo-1.x", level="dangerous", op_id="demo.x"
    )

    assert caution.kinds == ("Operators", "Admins")
    assert caution.rule == "install:routing[0]"
    # Operators may not approve another person's dangerous change today.
    assert dangerous.kinds == ("Admins",)


def test_no_entry_fits_gives_todays_rule() -> None:
    entries = [_entry("install:routing[0]", ("Admins",), category="network")]

    found = route(
        entries, category="storage", connector_id="demo-1.x", level="caution", op_id="demo.x"
    )

    assert found.rule == DEFAULT_RULE
    assert found.kinds == ("Operators", "SA-Operators", "Admins", "Approvers")


def test_no_category_matches_only_entries_without_one() -> None:
    entries = [
        _entry("install:routing[0]", ("Admins",), category="network"),
        _entry("install:routing[1]", ("SA-Operators",), risk="caution"),
    ]

    found = route(entries, category=None, connector_id="demo-1.x", level="caution", op_id="x")

    assert found.rule == "install:routing[1]"


def test_the_first_fitting_entry_wins_and_the_tenant_comes_first() -> None:
    install = {"routing": [{"match": {"category": "network"}, "approvers": ["Admins"]}]}
    tenant = {"routing": [{"match": {"connector": "demo-*"}, "approvers": ["SA-Operators"]}]}

    entries = routes_from_documents(install, tenant)
    found = route(
        entries, category="network", connector_id="demo-1.x", level="caution", op_id="demo.x"
    )

    assert [e.ref for e in entries] == ["tenant:routing[0]", "install:routing[0]"]
    assert found == route(
        entries[:1], category="network", connector_id="demo-1.x", level="caution", op_id="demo.x"
    )
    assert found.kinds == ("SA-Operators",)


def test_a_risk_and_connector_entry_needs_both_to_fit() -> None:
    entries = [_entry("install:routing[0]", ("Admins",), connector="nsx-*", risk="destructive")]

    other_level = route(entries, category=None, connector_id="nsx-4", level="caution", op_id="x")
    other_connector = route(
        entries, category=None, connector_id="k8s-1", level="destructive", op_id="x"
    )
    both = route(entries, category=None, connector_id="nsx-4", level="destructive", op_id="x")

    assert other_level.rule == DEFAULT_RULE
    assert other_connector.rule == DEFAULT_RULE
    assert both.kinds == ("Admins",)


def test_an_operation_with_the_admin_role_floor_goes_to_admins_only() -> None:
    entries = [_entry("install:routing[0]", ("Operators", "Admins"), category="network")]

    found = route(
        entries,
        category="network",
        connector_id="demo-1.x",
        level="caution",
        op_id="targets.register",
    )

    assert found.kinds == ("Admins",)


@pytest.mark.parametrize(
    ("role", "sa", "approver", "kind"),
    [
        (TenantRole.READ_ONLY, False, False, "Readers"),
        (TenantRole.OPERATOR, False, False, "Operators"),
        (TenantRole.OPERATOR, True, False, "SA-Operators"),
        (TenantRole.TENANT_ADMIN, False, False, "Admins"),
        (TenantRole.READ_ONLY, False, True, "Approvers"),
    ],
)
def test_each_login_has_one_group_kind(
    role: TenantRole, sa: bool, approver: bool, kind: str
) -> None:
    assert kind_of(_person("someone", role=role, sa_operator=sa, approver=approver)) == kind


def test_today_kinds_never_hold_readers() -> None:
    for level in ("safe", "caution", "dangerous", "destructive", None, "unknown"):
        assert "Readers" not in today_kinds(level, "demo.x")


# ---------------------------------------------------------------------------
# The file: parse, import checks, diff, export
# ---------------------------------------------------------------------------


def test_an_unknown_group_kind_and_a_repeated_match_are_mistakes() -> None:
    unknown = parse_policy_file(
        b"policy: v1\nrouting:\n  - match: {category: network}\n    approvers: [NetTeam]\n"
    )
    repeated = parse_policy_file(
        b"policy: v1\nrouting:\n"
        b"  - match: {category: network}\n    approvers: [Admins]\n"
        b"  - match: {category: network}\n    approvers: [SA-Operators]\n"
    )
    empty_match = parse_policy_file(
        b"policy: v1\nrouting:\n  - match: {}\n    approvers: [Admins]\n"
    )

    assert unknown.model is None
    assert any("routing[0].approvers" in m and "does not exist" in m for m in unknown.mistakes)
    assert repeated.mistakes == [
        "`routing[1].match` repeats `routing[0]`; the first one always wins, so list each "
        "match once."
    ]
    assert any("routing[0].match" in m for m in empty_match.mistakes)


def test_routing_is_stored_in_its_order_and_the_order_counts() -> None:
    text = (
        b"policy: v1\nrouting:\n"
        b"  - match: {category: network}\n    approvers: [Admins]\n"
        b"  - match: {risk: dangerous}\n    approvers: [SA-Operators]\n"
    )
    swapped = (
        b"policy: v1\nrouting:\n"
        b"  - match: {risk: dangerous}\n    approvers: [SA-Operators]\n"
        b"  - match: {category: network}\n    approvers: [Admins]\n"
    )
    first, second = parse_policy_file(text).model, parse_policy_file(swapped).model
    assert first is not None and second is not None

    document = document_of(first)

    assert document["routing"][0] == {"match": {"category": "network"}, "approvers": ["Admins"]}
    assert canonical_sha256(document) != canonical_sha256(document_of(second))


def test_a_file_without_routing_keeps_its_stored_form() -> None:
    parsed = parse_policy_file(b"policy: v1\nclassifications: []\n")
    assert parsed.model is not None

    assert "routing" not in document_of(parsed.model)


def test_a_kind_that_may_approve_none_of_the_changes_is_a_mistake() -> None:
    mistakes = routing_mistakes(
        [{"match": {"category": "network"}, "approvers": ["Readers", "Admins"]}], DEFAULT_TAXONOMY
    )

    assert mistakes == [
        "`routing[0]` asks `Readers`, but `Readers` has no `approve` rule for these changes."
    ]


def test_a_change_nobody_may_approve_is_a_mistake() -> None:
    mistakes = routing_mistakes(
        [{"match": {"category": "network"}, "approvers": ["Operators"]}], DEFAULT_TAXONOMY
    )

    assert mistakes == [
        (
            "A dangerous change in category network can wait, but no role may approve it. "
            "It would wait until it expires."
        ),
        (
            "A destructive change in category network can wait, but no role may approve it. "
            "It would wait until it expires."
        ),
    ]


def test_an_entry_for_one_risk_level_is_checked_on_that_level_only() -> None:
    fine = routing_mistakes(
        [{"match": {"category": "network", "risk": "caution"}, "approvers": ["Operators"]}],
        DEFAULT_TAXONOMY,
    )
    wrong = routing_mistakes(
        [{"match": {"connector": "nsx-*", "risk": "destructive"}, "approvers": ["Approvers"]}],
        DEFAULT_TAXONOMY,
    )

    assert fine == []
    assert wrong == [
        "`routing[0]` asks `Approvers`, but `Approvers` has no `approve` rule for these changes.",
        (
            "A destructive change matched by `routing[0]` can wait, but no role may approve it. "
            "It would wait until it expires."
        ),
    ]


def test_an_unknown_category_or_risk_level_is_a_mistake() -> None:
    mistakes = routing_mistakes(
        [
            {"match": {"category": "nope"}, "approvers": ["Admins"]},
            {"match": {"risk": "extreme"}, "approvers": ["Admins"]},
        ],
        DEFAULT_TAXONOMY,
    )

    assert mistakes[0].startswith("`routing[0].match.category` `nope` is not in the taxonomy")
    assert mistakes[1].startswith("`routing[1].match.risk` `extreme` is not a risk level")


def test_check_document_runs_the_routing_checks() -> None:
    document = {
        "classifications": [],
        "routing": [{"match": {"risk": "safe"}, "approvers": ["Readers"]}],
    }

    mistakes, _ = check_document(document, scope="tenant", install_document=None, ops=[])

    assert "`routing[0]` asks `Readers`" in mistakes[0]


def _floored_op(level: str = "caution") -> KnownOp:
    """``targets.register``: only Admins may approve it (the ``tenant_admin`` role floor)."""
    return KnownOp(
        coords=OpCoordinates("targets-registry-1.x", "targets", "targets.register"),
        safety_level=level,
        shows_secret=False,
        caller_address=False,
    )


def _check_with_floored_op(routing: list[dict[str, Any]], **kwargs: Any) -> list[str]:
    document = {"classifications": kwargs.pop("classifications", []), "routing": routing}
    mistakes, _ = check_document(
        document, scope="install", install_document=None, ops=[_floored_op(**kwargs)]
    )
    return mistakes


def test_routing_an_admin_only_operation_to_other_kinds_is_a_mistake() -> None:
    mistakes = _check_with_floored_op(
        [{"match": {"risk": "caution"}, "approvers": ["SA-Operators"]}]
    )

    assert mistakes == [
        (
            "`routing[0]` fits `targets.register` of connector `targets-registry-1.x`, but only "
            "MEHO-Admins may approve it and the entry does not ask them. It would wait until it "
            "expires."
        )
    ]


def test_routing_an_admin_only_operation_by_its_category_is_checked() -> None:
    classifications = [{"match": {"connector": "targets-*"}, "category": "identity"}]

    wrong = _check_with_floored_op(
        [{"match": {"category": "identity"}, "approvers": ["SA-Operators"]}],
        classifications=classifications,
    )
    other_category = _check_with_floored_op(
        [{"match": {"category": "network"}, "approvers": ["SA-Operators"]}],
        classifications=classifications,
    )

    assert len(wrong) == 1 and "`routing[0]` fits `targets.register`" in wrong[0]
    assert other_category == []


def test_an_entry_that_asks_admins_or_does_not_fit_the_operation_is_fine() -> None:
    with_admins = _check_with_floored_op(
        [{"match": {"connector": "targets-*"}, "approvers": ["SA-Operators", "Admins"]}]
    )
    other_connector = _check_with_floored_op(
        [{"match": {"connector": "nsx-*"}, "approvers": ["SA-Operators"]}]
    )
    lower_risk = _check_with_floored_op(
        [{"match": {"risk": "caution"}, "approvers": ["SA-Operators"]}], level="dangerous"
    )

    assert with_admins == []
    assert other_connector == []
    assert lower_risk == []


def test_an_earlier_entry_that_always_fits_shadows_a_later_one() -> None:
    mistakes = _check_with_floored_op(
        [
            {"match": {"connector": "targets-*"}, "approvers": ["Admins"]},
            {"match": {"risk": "caution"}, "approvers": ["SA-Operators"]},
        ]
    )

    assert mistakes == []


def test_an_earlier_entry_with_a_risk_level_shadows_a_later_one_at_that_level() -> None:
    mistakes = _check_with_floored_op(
        [
            {"match": {"connector": "targets-*", "risk": "caution"}, "approvers": ["Admins"]},
            {"match": {"risk": "caution"}, "approvers": ["SA-Operators"]},
        ]
    )

    assert mistakes == []


def test_a_later_entry_is_checked_at_the_levels_an_earlier_one_does_not_fit() -> None:
    mistakes = _check_with_floored_op(
        [
            {"match": {"connector": "targets-*", "risk": "caution"}, "approvers": ["Admins"]},
            {"match": {"connector": "targets-*"}, "approvers": ["SA-Operators"]},
        ]
    )

    assert len(mistakes) == 1 and mistakes[0].startswith("`routing[1]` fits `targets.register`")


def test_the_diff_names_added_changed_and_removed_routing_entries() -> None:
    before = [
        {"match": {"category": "network"}, "approvers": ["Admins"]},
        {"match": {"category": "storage"}, "approvers": ["Admins"]},
    ]
    after = [
        {"match": {"category": "network"}, "approvers": ["SA-Operators", "Admins"]},
        {"match": {"category": "identity"}, "approvers": ["Admins"]},
    ]

    diff = _routing_diff(before, after)

    assert diff["added"] == [{"entry": after[1], "index": 1}]
    assert diff["changed"] == [{"before": before[0], "after": after[0], "index": 0, "was": 0}]
    assert diff["removed"] == [{"entry": before[1], "index": 1}]
    assert _routing_diff(before, before) == {"added": [], "changed": [], "removed": []}


@pytest.mark.asyncio
async def test_export_writes_the_routing_back() -> None:
    await _store_policy(_NETWORK_ONLY_SA)

    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="install", tenant_id=None)

    parsed = parse_policy_file(text.encode())
    assert parsed.model is not None
    assert document_of(parsed.model)["routing"] == _NETWORK_ONLY_SA["routing"]


# ---------------------------------------------------------------------------
# The shipped "separated duties" file
# ---------------------------------------------------------------------------

_DEPLOY_COPY = (
    Path(__file__).resolve().parents[2] / "deploy" / "policy-profiles" / "separated-duties.yaml"
)


def _packaged() -> bytes:
    return (files("meho_backplane.policy") / "profiles" / "separated-duties.yaml").read_bytes()


def test_the_two_copies_of_separated_duties_are_the_same() -> None:
    assert _DEPLOY_COPY.read_bytes() == _packaged()


@pytest.mark.parametrize("scope", ["install", "tenant"])
def test_separated_duties_passes_the_import_checks(scope: str) -> None:
    parsed = parse_policy_file(_packaged())
    assert parsed.mistakes == []
    assert parsed.model is not None

    mistakes, _ = check_document(
        document_of(parsed.model),
        scope=scope,  # type: ignore[arg-type]
        install_document=None,
        ops=[],
    )

    assert mistakes == []


def test_separated_duties_routes_network_to_sa_operators_and_admins() -> None:
    parsed = parse_policy_file(_packaged())
    assert parsed.model is not None
    entries = routes_from_documents(document_of(parsed.model), None)

    network = route(
        entries, category="network", connector_id="pfsense-ssh-1", level="caution", op_id="x"
    )
    identity = route(
        entries, category="identity", connector_id="keycloak-admin-26", level="caution", op_id="x"
    )
    compute = route(entries, category="compute", connector_id="k8s-1", level="caution", op_id="x")

    assert network.kinds == ("SA-Operators", "Admins")
    assert identity.kinds == ("Admins",)
    assert compute.rule == DEFAULT_RULE


# ---------------------------------------------------------------------------
# Park: what the request stores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_waiting_request_stores_the_routed_kinds_and_never_a_person() -> None:
    await _store_policy(_NETWORK_ONLY_SA)

    row = await _load(await _park("caution"))

    assert row.approver_roles == ["SA-Operators", "Admins"]
    assert row.approver_rule == "install:routing[0]"
    assert set(row.approver_roles) <= set(GROUP_KINDS)
    assert row.principal_sub not in row.approver_roles


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_the_tenants_own_routing_wins() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    await _store_policy(
        {
            "classifications": [],
            "routing": [{"match": {"risk": "caution"}, "approvers": ["Admins"]}],
        },
        tenant_id=_TENANT_A,
    )

    row = await _load(await _park("caution"))

    assert row.approver_roles == ["Admins"]
    assert row.approver_rule == "tenant:routing[0]"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_without_a_policy_the_stored_kinds_are_todays_rule() -> None:
    row = await _load(await _park("dangerous"))

    assert row.approver_roles == ["SA-Operators", "Admins"]
    assert row.approver_rule == DEFAULT_RULE


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_rights_change_is_not_routed() -> None:
    await _store_policy(_NETWORK_ONLY_SA)

    row = await _load(await _park("caution", rights_change=True))

    assert row.approver_roles is None
    assert row.approver_rule is None


@pytest.mark.asyncio
async def test_with_the_switch_off_nothing_is_stored() -> None:
    await _store_policy(_NETWORK_ONLY_SA)

    row = await _load(await _park("caution"))

    assert row.approver_roles is None
    assert row.approver_rule is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_bad_stored_routing_refuses_the_park() -> None:
    await _store_policy({"routing": "not a list"})

    with pytest.raises(ValueError, match="`routing` must be a list"):
        await _park("caution")


# ---------------------------------------------------------------------------
# The click
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_person_outside_the_stored_kinds_gets_one_sentence() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")
    operator = _person(_OTHER)

    with pytest.raises(ApprovalRoutingError) as caught:
        await _approve(request_id, operator)

    expected = (
        "Only MEHO-SA-Operators or MEHO-Admins may approve this change "
        "(routing rule `install:routing[0]`). You are in MEHO-Operators."
    )
    assert caught.value.reason_text == expected
    assert isinstance(caught.value, ApprovalGroupForbiddenError)
    row = await _load(request_id)
    assert row.status == ApprovalRequestStatus.PENDING.value
    right = approve_right(operator, row)
    assert (right.may_approve, right.reason, right.message) == (
        False,
        ApproveRightReason.NOT_ROUTED,
        expected,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_person_in_the_stored_kinds_approves() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")

    approved = await _approve(request_id, _person(_OTHER, sa_operator=True))

    assert approved.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_routing_also_narrows_an_own_click() -> None:
    await _store_policy(
        {
            "classifications": [],
            "routing": [{"match": {"category": "network"}, "approvers": ["Admins"]}],
        }
    )
    request_id = await _park("caution")
    # A MEHO-SA-Operator may approve their own change today; routing sends it to Admins.
    requester_sa = _person(_REQUESTER, sa_operator=True)

    with pytest.raises(ApprovalRoutingError):
        await _approve(request_id, requester_sa)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_todays_checks_still_run_inside_the_stored_kinds() -> None:
    await _store_policy(
        {
            "classifications": [],
            "routing": [
                {
                    "match": {"category": "network"},
                    "approvers": ["Operators", "SA-Operators", "Admins"],
                }
            ],
        }
    )
    request_id = await _park("caution")

    # The requester is an Operator, in the stored kinds, but may not approve their own change.
    right = approve_right(_person(_REQUESTER), await _load(request_id))

    assert right.reason == ApproveRightReason.OWN_CHANGE
    assert right.may_approve is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_with_the_default_rule_an_operator_approves_a_caution_change_as_today() -> None:
    request_id = await _park("caution")

    approved = await _approve(request_id, _person(_OTHER))

    assert approved.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_an_old_request_without_stored_kinds_keeps_todays_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")  # switch off: nothing stored
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()

    approved = await _approve(request_id, _person(_OTHER))

    assert approved.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_with_the_switch_off_stored_kinds_are_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")
    _routing_off(monkeypatch)

    approved = await _approve(request_id, _person(_OTHER))

    assert approved.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_a_stored_value_that_is_not_a_list_lets_nobody_approve() -> None:
    request_id = await _park("caution")
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        row.approver_roles = {"kinds": "Admins"}  # type: ignore[assignment]
        await session.commit()

    with pytest.raises(ApprovalRoutingError, match="No group may approve this change"):
        await _approve(request_id, _person(_OTHER, role=TenantRole.TENANT_ADMIN))


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_who_can_approve_names_the_groups() -> None:
    await _store_policy(_NETWORK_ONLY_SA)
    row = await _load(await _park("caution"))

    assert who_can_approve(row.approver_roles) == "Members of MEHO-SA-Operators or MEHO-Admins."
    assert who_can_approve(None) is None
    assert who_can_approve([]) == (
        "Nobody: no group may approve this change, so it waits until it expires."
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_routing_on")
async def test_rest_approve_answers_403_with_the_sentence(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    await _store_policy(_NETWORK_ONLY_SA)
    request_id = await _park("caution")
    token = _rest_token(key, _OTHER, groups=["MEHO-Operators"])

    response = await _post(ac, "approve", request_id, token)

    assert response.status_code == 403
    assert "Only MEHO-SA-Operators or MEHO-Admins may approve this change" in response.text
    assert _fake_resume == []
