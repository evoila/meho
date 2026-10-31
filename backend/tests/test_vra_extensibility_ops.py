# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""ABX actions and event subscriptions on VCF Automation 9 and Aria Automation 8.

The five ops are written once (``vmware_shared.vra_extensibility``) and
registered on both connectors. Each test drives an op through the real
dispatcher against a respx-mocked appliance. All names and ids are made up.

What is proven here:

* the action list and the run read never return the script source, and
  hide secret values in inputs, outputs and the log;
* the approval card (preview builder) of both destructive changes reads the
  object, fills the blast-radius block, and refuses a missing object;
* a destructive change is refused without a matching ``preview_hash``; the
  full flow works: preview, wait, a second person approves, then it runs;
* when the action changed after the approval, nothing runs;
* each subscription change sends the right call, a second run is
  ``unchanged``, a system subscription is not changed, and a vendor error
  reaches the answer;
* the vRA 8 connector sends the same calls with its own login.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vcf_automation import (
    VCFA_CONNECTOR_ID,
    VcfAutomationConnector,
    register_vcfa_governance_operations,
)
from meho_backplane.connectors.vra8 import Vra8Connector, register_vra8_governance_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import (
    PreviewContext,
    blast_radius_missing_reason,
    build_proposed_effect,
)
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TARGET_NAME,
    TENANT_ID,
    TENANT_TOKEN,
    mount_logins,
    resolved_target,
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

_ACTIONS = "/abx/api/resources/actions"
_RUNS = "/abx/api/resources/action-runs"
_SUBS = "/event-broker/api/subscriptions"
_TOPICS = "/event-broker/api/topics"

_APPROVER = Operator(
    sub="vcfa-governance-approver",
    name="Second Person",
    email=None,
    raw_jwt="<approver-raw-jwt>",
    tenant_id=TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)


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


def _action(**overrides: Any) -> dict[str, Any]:
    action = {
        "id": "act-1",
        "name": "<action-name>",
        "actionType": "SCRIPT",
        "runtime": "python",
        "projectId": "p-1",
        "contentId": "content-1",
        "source": "def handler(context, inputs):\n    return {'token': '<not-a-real-token>'}",
        "compressedContent": "<zip-bytes>",
        "configuration": {"const_password": "<not-a-real-value>"},
        "inputs": {"vmName": "<default-name>", "apiKey": "<not-a-real-key>"},
    }
    return {**action, **overrides}


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": VCFA_CONNECTOR_ID,
        "op_id": op_id,
        "target": TARGET_NAME,
        "params": params,
    }


@dataclass
class _FakeDescriptor:
    op_id: str


async def _card(op_id: str, params: dict[str, Any], connector: Any) -> Any:
    """The approval card (proposed effect) the change would wait with."""
    return await build_proposed_effect(
        PreviewContext(
            descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=OPERATOR,
            target=await resolved_target(),
            params=params,
            connector_id=VCFA_CONNECTOR_ID,
        )
    )


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


# ---------------------------------------------------------------------------
# ABX: reads
# ---------------------------------------------------------------------------


async def test_action_list_never_returns_the_source(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_ACTIONS).respond(200, json={"content": [_action()], "totalElements": 1})
        result = await run("vcfa.tenant.abx.action.list", {"name": "<action-name>", "size": 5})
    out = result["result"]
    row = out["actions"][0]
    assert row["inputNames"] == ["apiKey", "vmName"]
    text = json.dumps(out)
    for hidden in ("def handler", "<zip-bytes>", "<not-a-real", "<default-name>"):
        assert hidden not in text
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"
    assert dict(request.url.params) == {"nameCriteria": "<action-name>", "size": "5"}


async def test_run_get_hides_secrets_and_keeps_the_log_tail(
    vcfa: VcfAutomationConnector,
) -> None:
    run_row = {
        "id": "run-1",
        "actionId": "act-1",
        "runState": "FAILED",
        "errorMessage": "connection refused",
        "source": "print('<script-source>')",
        "inputs": {"vmName": "<vm-name>", "password": "<not-a-real-value>"},
        "outputs": {"result": "<value>"},
        "logs": 'line 1\nline 2\n{"token": "<not-a-real-token>"}\nline 4',
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_RUNS}/run-1").respond(200, json=run_row)
        result = await run("vcfa.tenant.abx.run.get", {"id": "run-1", "log_tail": 2})
    out = result["result"]
    assert out["done"] is True
    assert out["errorMessage"] == "connection refused"
    assert out["inputs"] == {"vmName": "<vm-name>", "password": SECRET_FIELD_MARKER}
    assert out["log_lines_total"] == 4
    assert out["log_lines"][-1] == "line 4"
    assert len(out["log_lines"]) == 2
    text = json.dumps(out)
    assert "<not-a-real" not in text
    assert "<script-source>" not in text


# ---------------------------------------------------------------------------
# ABX: run an action (destructive)
# ---------------------------------------------------------------------------

_RUN_PARAMS: dict[str, Any] = {
    "id": "act-1",
    "project_id": "p-1",
    "inputs": {"vmName": "<vm-name>"},
}


async def test_run_card_fills_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        effect = await _card("vcfa.tenant.abx.action.run", _RUN_PARAMS, vcfa)
    preview = effect["preview"]
    assert blast_radius_missing_reason(preview) is None
    block = preview["blast_radius"]
    assert block["object"]["name"] == "<action-name>"
    assert block["object"]["content_id"] == "content-1"
    assert block["object"]["input_names"] == ["vmName"]
    assert block["children"][0]["kind"] == "script_effects"
    assert "cannot be undone" in block["irreversibility"]
    assert read.calls.last.request.url.params["projectId"] == "p-1"
    assert "<not-a-real" not in json.dumps(effect)


@pytest.mark.parametrize(
    ("params", "status", "expected"),
    [
        (_RUN_PARAMS, 404, "found no ABX action"),
        ({**_RUN_PARAMS, "inputs": {"password": "<not-a-real-value>"}}, 200, "look like secrets"),
    ],
    ids=["missing-action", "secret-input"],
)
async def test_run_card_refuses(
    vcfa: VcfAutomationConnector, params: dict[str, Any], status: int, expected: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ACTIONS}/act-1").respond(status, json=_action())
        effect = await _card("vcfa.tenant.abx.action.run", params, vcfa)
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert expected in reason
    assert "<not-a-real-value>" not in reason


async def test_run_without_preview_hash_is_refused(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        start = mock.post(f"{_ACTIONS}/act-1/action-runs").respond(200, json={"id": "run-1"})
        call = await call_operation(OPERATOR, _args("vcfa.tenant.abx.action.run", _RUN_PARAMS))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not start.called
    assert await _approval_rows() == 0


async def _park_and_approve(op_id: str, params: dict[str, Any]) -> Any:
    """Preview, call, check it waits, let a second person approve; return the row."""
    args = _args(op_id, params)
    preview = await preview_operation(OPERATOR, args)
    assert preview["status"] == "ok", preview
    call = await call_operation(OPERATOR, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    request_id = uuid.UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=_APPROVER, params=None)
        await session.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        session.expunge(row)
    return row


async def _resume(row: Any) -> Any:
    return await resume_dispatch_after_approval(operator=_APPROVER, request=row, params=None)


async def test_run_waits_for_a_second_person_then_runs(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        start = mock.post(f"{_ACTIONS}/act-1/action-runs").respond(
            200, json={"id": "run-1", "actionId": "act-1", "runState": "CREATED"}
        )
        row = await _park_and_approve("vcfa.tenant.abx.action.run", _RUN_PARAMS)
        assert row.proposed_effect["safety_level"] == "destructive"
        assert row.proposed_effect["blast_radius"]["object"]["name"] == "<action-name>"
        assert not start.called
        resumed = await _resume(row)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "started"
    assert resumed.result["run"]["id"] == "run-1"
    assert _body(start) == {"inputs": {"vmName": "<vm-name>"}, "projectId": "p-1"}
    assert start.call_count == 1


async def test_run_stops_when_the_action_changed_after_approval(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        start = mock.post(f"{_ACTIONS}/act-1/action-runs").respond(200, json={"id": "run-1"})
        row = await _park_and_approve("vcfa.tenant.abx.action.run", _RUN_PARAMS)
        read.respond(200, json=_action(contentId="content-2"))
        resumed = await _resume(row)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "precondition_failed"
    assert "content_id" in resumed.result["guidance"]
    assert not start.called


async def test_run_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        mock.post(f"{_ACTIONS}/act-1/action-runs").respond(
            400, json={"message": "Project p-1 has no ABX provider"}
        )
        result = await run("vcfa.tenant.abx.action.run", _RUN_PARAMS)
    assert result["status"] == "error"
    assert "has no ABX provider" in json.dumps(result)


# ---------------------------------------------------------------------------
# Event subscriptions
# ---------------------------------------------------------------------------


def _sub(**overrides: Any) -> dict[str, Any]:
    sub = {
        "id": "sub-1",
        "name": "<subscription-name>",
        "eventTopicId": "compute.provision.post",
        "runnableType": "extensibility.abx",
        "runnableId": "act-1",
        "blocking": False,
        "disabled": False,
        "priority": 10,
        "constraints": {"projectId": ["p-1"]},
        "system": False,
    }
    return {**sub, **overrides}


_CREATE: dict[str, Any] = {
    "action": "create",
    "name": "<subscription-name>",
    "event_topic_id": "compute.provision.post",
    "runnable_type": "extensibility.abx",
    "runnable_id": "act-1",
    "project_ids": ["p-1"],
}


async def test_subscription_list_returns_flat_rows(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SUBS).respond(200, json={"content": [_sub()], "totalElements": 1})
        result = await run("vcfa.tenant.subscription.list", {"size": 50})
    row = result["result"]["subscriptions"][0]
    assert row["projectIds"] == ["p-1"]
    assert row["runnableType"] == "extensibility.abx"


async def test_subscription_card_for_a_create_reads_topic_and_action(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SUBS).respond(200, json={"content": [], "last": True})
        mock.get(f"{_TOPICS}/compute.provision.post").respond(
            200, json={"name": "Compute post provision", "blockable": True}
        )
        mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        effect = await _card("vcfa.tenant.subscription.set", _CREATE, vcfa)
    block = effect["preview"]["blast_radius"]
    assert blast_radius_missing_reason(effect["preview"]) is None
    assert block["object"]["name"] == "<subscription-name>"
    assert block["children"][0]["blockable"] is True
    assert block["children"][1]["name"] == "<action-name>"


async def test_subscription_card_refuses_a_missing_subscription(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_SUBS}/sub-9").respond(404)
        effect = await _card(
            "vcfa.tenant.subscription.set", {"action": "delete", "id": "sub-9"}, vcfa
        )
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert "found no event subscription" in reason


async def test_subscription_delete_card_says_permanent(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_SUBS}/sub-1").respond(200, json=_sub())
        effect = await _card(
            "vcfa.tenant.subscription.set", {"action": "delete", "id": "sub-1"}, vcfa
        )
    block = effect["preview"]["blast_radius"]
    assert block["irreversibility"].startswith("permanent")
    assert block["object"]["runnable_id"] == "act-1"


async def test_subscription_without_preview_hash_is_refused(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        delete = mock.delete(f"{_SUBS}/sub-1").respond(204)
        call = await call_operation(
            OPERATOR, _args("vcfa.tenant.subscription.set", {"action": "delete", "id": "sub-1"})
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not delete.called


async def test_subscription_disable_waits_then_runs(vcfa: VcfAutomationConnector) -> None:
    params = {"action": "disable", "id": "sub-1"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_SUBS}/sub-1").respond(200, json=_sub())
        patch = mock.patch(f"{_SUBS}/sub-1").respond(200, json=_sub(disabled=True))
        row = await _park_and_approve("vcfa.tenant.subscription.set", params)
        assert not patch.called
        resumed = await _resume(row)
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "disabled"
    assert _body(patch) == {"disabled": True}


async def test_subscription_create_sends_the_subscription(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SUBS).respond(200, json={"content": [], "last": True})
        post = mock.post(_SUBS).respond(201, json=_sub())
        result = await run("vcfa.tenant.subscription.set", _CREATE)
    assert result["result"]["status"] == "created", result
    assert _body(post) == {
        "type": "RUNNABLE",
        "disabled": False,
        "name": "<subscription-name>",
        "eventTopicId": "compute.provision.post",
        "runnableType": "extensibility.abx",
        "runnableId": "act-1",
        "constraints": {"projectId": ["p-1"]},
    }


async def test_subscription_create_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SUBS).respond(200, json={"content": [_sub()], "last": True})
        post = mock.post(_SUBS).respond(201, json=_sub())
        result = await run("vcfa.tenant.subscription.set", _CREATE)
    assert result["result"]["status"] == "unchanged"
    assert not post.called


@pytest.mark.parametrize(
    ("params", "current", "status", "patch_body"),
    [
        ({"action": "update", "id": "sub-1", "priority": 1}, _sub(), "changed", {"priority": 1}),
        ({"action": "update", "id": "sub-1", "priority": 10}, _sub(), "unchanged", None),
        ({"action": "enable", "id": "sub-1"}, _sub(disabled=True), "enabled", {"disabled": False}),
        ({"action": "disable", "id": "sub-1"}, _sub(system=True), "invalid_request", None),
    ],
    ids=["update", "update-same", "enable", "system"],
)
async def test_subscription_changes(
    vcfa: VcfAutomationConnector,
    params: dict[str, Any],
    current: dict[str, Any],
    status: str,
    patch_body: dict[str, Any] | None,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_SUBS}/sub-1").respond(200, json=current)
        patch = mock.patch(f"{_SUBS}/sub-1").respond(200, json={})
        result = await run("vcfa.tenant.subscription.set", params)
    assert result["result"]["status"] == status, result
    if patch_body is None:
        assert not patch.called
    else:
        assert _body(patch) == patch_body


async def test_subscription_delete_and_vendor_error(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_SUBS}/sub-1").respond(200, json=_sub())
        delete = mock.delete(f"{_SUBS}/sub-1").respond(204)
        deleted = await run("vcfa.tenant.subscription.set", {"action": "delete", "id": "sub-1"})
        delete.respond(409, json={"message": "Subscription is in use by a running event"})
        failed = await run("vcfa.tenant.subscription.set", {"action": "delete", "id": "sub-1"})
    assert deleted["result"]["status"] == "deleted"
    assert failed["status"] == "error"
    assert "in use by a running event" in json.dumps(failed)


async def test_subscription_schema_needs_an_id_for_a_change(
    vcfa: VcfAutomationConnector,
) -> None:
    result = await run("vcfa.tenant.subscription.set", {"action": "delete"})
    assert result["status"] != "ok"


# ---------------------------------------------------------------------------
# Aria Automation 8: the same ops with the vRA 8 login
# ---------------------------------------------------------------------------


async def test_vra8_reads_use_the_vra8_bearer(vra8: Vra8Connector) -> None:
    async with respx.mock(base_url=VRA_BASE_URL, assert_all_called=False) as mock:
        mount_vra_logins(mock)
        actions = mock.get(_ACTIONS).respond(200, json={"content": [_action()]})
        subs = mock.get(_SUBS).respond(200, json={"content": [_sub()]})
        listed = await run_vra8("vcfa.vra8.abx.action.list", {})
        subscriptions = await run_vra8("vcfa.vra8.subscription.list", {})
    assert listed.status == "ok", listed.error
    assert subscriptions.status == "ok", subscriptions.error
    for route in (actions, subs):
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {VRA_BEARER}"
    assert "def handler" not in json.dumps(listed.result)


async def test_vra8_run_and_patch(vra8: Vra8Connector) -> None:
    async with respx.mock(base_url=VRA_BASE_URL, assert_all_called=False) as mock:
        mount_vra_logins(mock)
        mock.get(f"{_ACTIONS}/act-1").respond(200, json=_action())
        start = mock.post(f"{_ACTIONS}/act-1/action-runs").respond(200, json={"id": "run-1"})
        mock.get(f"{_SUBS}/sub-1").respond(200, json=_sub())
        patch = mock.patch(f"{_SUBS}/sub-1").respond(200, json=_sub(disabled=True))
        started = await run_vra8("vcfa.vra8.abx.action.run", _RUN_PARAMS)
        disabled = await run_vra8(
            "vcfa.vra8.subscription.set", {"action": "disable", "id": "sub-1"}
        )
    assert started.status == "ok", started.error
    assert started.result["status"] == "started"
    assert "vcfa.vra8.abx.run.get" in started.result["guidance"]
    assert disabled.result["status"] == "disabled"
    for route in (start, patch):
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {VRA_BEARER}"
