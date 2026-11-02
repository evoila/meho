# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approvals and policies on VCF Automation 9 and Aria Automation 8.

The four ops are written once (``vmware_shared.vra_governance``) and
registered on both connectors, together with the five ABX and subscription
ops (``test_vra_extensibility_ops.py``). Each test drives an op through the
real dispatcher against a respx-mocked appliance. All names and ids are made
up.

What is proven here:

* the risk level and approval flag of all nine ops, on both connectors;
* both registrars land the rows and the two groups with their texts;
* the approval list and the decision (waits for approval, sends the
  decision, sends nothing when the approval no longer waits, passes a vendor
  error on);
* the policy list and the policy change (waits for approval, dry run first,
  safe to run twice, refuses bad input, a failed dry run saves nothing);
* the vRA 8 connector sends the same calls with its own login.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_GOVERNANCE_OPS,
    VcfAutomationConnector,
    register_vcfa_governance_operations,
)
from meho_backplane.connectors.vcf_automation.governance_ops import (
    VCFA_GOVERNANCE_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_shared.vra_governance import (
    VraChangeRefusedError,
    path_segment,
)
from meho_backplane.connectors.vra8 import (
    VRA8_GOVERNANCE_OPS,
    Vra8Connector,
    register_vra8_governance_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from tests._vcfa_provisioning_support import (
    BASE_URL,
    TENANT_TOKEN,
    mount_logins,
    run,
    seed_target,
    wire_connector,
)
from tests._vra_governance_support import (
    VRA_BASE_URL,
    VRA_BEARER,
    mount_vra_logins,
    run_vra8,
    wire_vra8,
)

_APPROVALS = "/approval/api/approvals"
_POLICIES = "/policy/api/policies"

#: The task table: op name -> (safety level, requires approval).
_TIERS: dict[str, tuple[str, bool]] = {
    "approval.list": ("safe", False),
    "approval.decide": ("dangerous", True),
    "policy.list": ("safe", False),
    "policy.set": ("dangerous", True),
    "abx.action.list": ("safe", False),
    "abx.action.run": ("destructive", True),
    "abx.run.get": ("safe", False),
    "subscription.list": ("safe", False),
    "subscription.set": ("destructive", True),
}


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await register_vcfa_governance_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


@pytest.fixture
async def vra8() -> AsyncIterator[Vra8Connector]:
    set_default_reducer(PassThroughReducer())
    await register_vra8_governance_operations()
    connector = wire_vra8()
    yield connector
    await connector.aclose()


def _body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def _approval(status: str = "PENDING") -> dict[str, Any]:
    return {
        "id": "ap-1",
        "status": status,
        "action": "Create",
        "requestId": "r-1",
        "requestedBy": "<user-name>",
        "deploymentId": "d-1",
        "deploymentName": "<deployment-name>",
        "projectId": "p-1",
        "projectName": "<project-name>",
        "requestDetails": {"catalogDetails": {"id": "ci-1", "name": "<item-name>"}},
        "appliedPolicies": [
            {
                "policyName": "<policy-name>",
                "approvers": [
                    {"approver": "<approver-a>", "status": "PENDING"},
                    {"approver": "<approver-b>", "status": "APPROVED"},
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Metadata and registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ops", "prefix"),
    [(VCFA_GOVERNANCE_OPS, "vcfa.tenant"), (VRA8_GOVERNANCE_OPS, "vcfa.vra8")],
    ids=["vcfa-rest", "vcfa-vra8"],
)
def test_safety_and_approval_follow_the_table(ops: tuple[Any, ...], prefix: str) -> None:
    expected = {f"{prefix}.{name}": tier for name, tier in _TIERS.items()}
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in ops} == expected


def test_group_texts_name_every_op_and_make_no_group_safety_claim() -> None:
    text = " ".join(VCFA_GOVERNANCE_WHEN_TO_USE_BY_GROUP.values())
    for name in _TIERS:
        assert f"vcfa.tenant.{name}" in text
    assert "all reads" not in text.lower()


async def test_vcfa_rows_and_groups_are_registered(vcfa: VcfAutomationConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row for row in (await session.execute(select(EndpointDescriptor))).scalars()
        }
        groups = {
            row.group_key: row for row in (await session.execute(select(OperationGroup))).scalars()
        }
    for op in VCFA_GOVERNANCE_OPS:
        row = rows[op.op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (op.safety_level, op.requires_approval)
        assert row.impl_id == "vcfa-rest"
    for key in ("vcfa-tenant-governance", "vcfa-tenant-extensibility"):
        assert groups[key].when_to_use == VCFA_GOVERNANCE_WHEN_TO_USE_BY_GROUP[key]


async def test_vra8_rows_are_registered(vra8: Vra8Connector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row for row in (await session.execute(select(EndpointDescriptor))).scalars()
        }
    for op in VRA8_GOVERNANCE_OPS:
        assert rows[op.op_id].impl_id == "vcfa-vra8"


@pytest.mark.parametrize("value", ["", " ", ".", ".."])
def test_path_segment_refuses_a_value_that_is_not_an_id(value: str) -> None:
    with pytest.raises(VraChangeRefusedError):
        path_segment(value, "id")


def test_path_segment_encodes_special_characters() -> None:
    assert path_segment("a/b#c", "id") == "a%2Fb%23c"


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


async def test_approval_list_returns_flat_rows(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_APPROVALS).respond(
            200, json={"content": [_approval()], "totalElements": 1}
        )
        result = await run("vcfa.tenant.approval.list", {"state": "PENDING", "size": 20})
    out = result["result"]
    assert out["totalElements"] == 1
    row = out["approvals"][0]
    assert row["catalogItemName"] == "<item-name>"
    assert row["policyNames"] == ["<policy-name>"]
    assert row["pendingApprovers"] == ["<approver-a>"]
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"
    assert dict(request.url.params) == {"requestState": "PENDING", "size": "20"}


async def test_approval_decide_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        action = mock.post(f"{_APPROVALS}/action").respond(200, json={})
        result = await run(
            "vcfa.tenant.approval.decide", {"id": "ap-1", "decision": "APPROVE"}, approved=False
        )
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not action.called


async def test_approval_decide_sends_the_decision(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_APPROVALS}/ap-1").mock(
            side_effect=[
                respx.MockResponse(200, json=_approval()),
                respx.MockResponse(200, json=_approval("APPROVED")),
            ]
        )
        action = mock.post(f"{_APPROVALS}/action").respond(200, json={"itemId": "ap-1"})
        result = await run(
            "vcfa.tenant.approval.decide",
            {"id": "ap-1", "decision": "REJECT", "comment": "<comment>"},
        )
    out = result["result"]
    assert out["status"] == "decided", out
    assert out["decision"] == "REJECT"
    assert out["approval"]["status"] == "APPROVED"
    assert _body(action) == {"itemId": "ap-1", "action": "REJECT", "comment": "<comment>"}


async def test_approval_decide_sends_nothing_when_it_no_longer_waits(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_APPROVALS}/ap-1").respond(200, json=_approval("EXPIRED"))
        action = mock.post(f"{_APPROVALS}/action").respond(200, json={})
        result = await run("vcfa.tenant.approval.decide", {"id": "ap-1", "decision": "APPROVE"})
    assert result["result"]["status"] == "not_pending"
    assert "EXPIRED" in result["result"]["guidance"]
    assert not action.called


async def test_approval_decide_vendor_error_reaches_the_answer(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_APPROVALS}/ap-1").respond(200, json=_approval())
        mock.post(f"{_APPROVALS}/action").respond(
            403, json={"message": "User is not an approver of this request"}
        )
        result = await run("vcfa.tenant.approval.decide", {"id": "ap-1", "decision": "APPROVE"})
    assert result["status"] == "error"
    assert "not an approver" in json.dumps(result)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

_LEASE = "com.vmware.policy.deployment.lease"
_POLICY_PARAMS: dict[str, Any] = {
    "name": "<policy-name>",
    "type_id": _LEASE,
    "project_id": "p-1",
    "enforcement_type": "HARD",
    "definition": {"leaseTermMax": 30},
}


def _policy(**overrides: Any) -> dict[str, Any]:
    policy = {
        "id": "pol-1",
        "name": "<policy-name>",
        "typeId": _LEASE,
        "projectId": "p-1",
        "enforcementType": "HARD",
        "definition": {"leaseTermMax": 7},
        "orgId": "o-1",
    }
    return {**policy, **overrides}


async def test_policy_list_returns_flat_rows(vcfa: VcfAutomationConnector) -> None:
    secret_policy = _policy(definition={"leaseTermMax": 7, "apiToken": "<not-a-real-token>"})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_POLICIES).respond(
            200, json={"content": [secret_policy], "totalElements": 1}
        )
        result = await run("vcfa.tenant.policy.list", {"with_definition": True, "$top": 10})
    out = result["result"]
    assert out["policies"][0]["typeId"] == _LEASE
    assert "<not-a-real-token>" not in json.dumps(out)
    assert dict(route.calls.last.request.url.params) == {
        "expandDefinition": "true",
        "$top": "10",
    }


def _mount_policy_set(
    mock: respx.MockRouter, existing: list[dict[str, Any]], *, dry_run_status: int = 200
) -> tuple[respx.Route, respx.Route]:
    mock.get(_POLICIES).respond(200, json={"content": existing})
    for policy in existing:
        mock.get(f"{_POLICIES}/{policy['id']}").respond(200, json=policy)
    dry = mock.post(_POLICIES, params={"dryRun": "true"}).respond(
        dry_run_status, json={"message": "leaseTermMax must be at least 1"}
    )
    save = mock.post(_POLICIES, params={"dryRun": "false"}).respond(
        201, json=_policy(definition={"leaseTermMax": 30})
    )
    return dry, save


async def test_policy_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        dry, save = _mount_policy_set(mock, [])
        result = await run("vcfa.tenant.policy.set", _POLICY_PARAMS, approved=False)
    assert result["status"] != "ok"
    assert not dry.called and not save.called


async def test_policy_set_creates_after_a_dry_run(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        dry, save = _mount_policy_set(mock, [])
        result = await run("vcfa.tenant.policy.set", _POLICY_PARAMS)
    out = result["result"]
    assert out["status"] == "created", out
    expected = {
        "name": "<policy-name>",
        "typeId": _LEASE,
        "projectId": "p-1",
        "enforcementType": "HARD",
        "definition": {"leaseTermMax": 30},
    }
    assert _body(dry) == expected
    assert _body(save) == expected


async def test_policy_set_changes_the_policy_with_the_same_name(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, save = _mount_policy_set(mock, [_policy()])
        result = await run("vcfa.tenant.policy.set", _POLICY_PARAMS)
    assert result["result"]["status"] == "changed"
    body = _body(save)
    assert body["id"] == "pol-1"
    assert body["definition"] == {"leaseTermMax": 30}
    assert body["typeId"] == _LEASE


async def test_policy_set_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    same = _policy(definition={"leaseTermMax": 30})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        dry, save = _mount_policy_set(mock, [same])
        result = await run("vcfa.tenant.policy.set", _POLICY_PARAMS)
    assert result["result"]["status"] == "unchanged"
    assert not dry.called and not save.called


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"scope_criteria": {"matchExpression": []}}, "not both"),
        ({"id": "pol-9"}, "no policy with id"),
        ({"id": "pol-1", "type_id": "com.vmware.policy.approval"}, "cannot change"),
    ],
    ids=["project-and-scope", "unknown-id", "other-type"],
)
async def test_policy_set_refuses_bad_input(
    vcfa: VcfAutomationConnector, change: dict[str, Any], expected: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        dry, save = _mount_policy_set(mock, [_policy()])
        mock.get(f"{_POLICIES}/pol-9").respond(404)
        result = await run("vcfa.tenant.policy.set", {**_POLICY_PARAMS, **change})
    out = result["result"]
    assert out["status"] == "invalid_request", out
    assert expected in out["guidance"]
    assert not dry.called and not save.called


async def test_policy_set_failed_dry_run_saves_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, save = _mount_policy_set(mock, [], dry_run_status=400)
        result = await run("vcfa.tenant.policy.set", _POLICY_PARAMS)
    assert result["status"] == "error"
    assert "leaseTermMax must be at least 1" in json.dumps(result)
    assert not save.called


# ---------------------------------------------------------------------------
# Aria Automation 8: the same ops with the vRA 8 login
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "path", "params"),
    [
        ("vcfa.vra8.approval.list", _APPROVALS, {}),
        ("vcfa.vra8.policy.list", _POLICIES, {}),
    ],
    ids=["approval-list", "policy-list"],
)
async def test_vra8_reads_use_the_vra8_bearer(
    vra8: Vra8Connector, op_id: str, path: str, params: dict[str, Any]
) -> None:
    async with respx.mock(base_url=VRA_BASE_URL, assert_all_called=False) as mock:
        mount_vra_logins(mock)
        route = mock.get(path).respond(200, json={"content": [], "totalElements": 0})
        result = await run_vra8(op_id, params)
    assert result.status == "ok", result.error
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {VRA_BEARER}"


async def test_vra8_approval_decide(vra8: Vra8Connector) -> None:
    async with respx.mock(base_url=VRA_BASE_URL, assert_all_called=False) as mock:
        mount_vra_logins(mock)
        mock.get(f"{_APPROVALS}/ap-1").respond(200, json=_approval())
        action = mock.post(f"{_APPROVALS}/action").respond(200, json={})
        result = await run_vra8("vcfa.vra8.approval.decide", {"id": "ap-1", "decision": "APPROVE"})
    assert result.status == "ok", result.error
    assert result.result["status"] == "decided"
    assert "vcfa.vra8.approval.list" in result.result["guidance"]
    assert action.calls.last.request.headers["Authorization"] == f"Bearer {VRA_BEARER}"
