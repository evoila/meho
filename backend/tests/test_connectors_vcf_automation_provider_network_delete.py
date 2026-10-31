# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vcfa.provider.network.delete``: the blast-radius card, the order rule and the run.

Pins: the card names the object, what goes with it and that it cannot be
undone; the card refuses a missing object and an object still in use (the
gateway before its regional networking); a call without a matching
``preview_hash`` is refused; a second person approves, then the ``DELETE``
runs once and answers ``accepted`` with the vendor's task id; the approved run
stops when the object changed after the card was made; the vendor's refusal
reaches the answer.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import approved_preview_var
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from tests._vcfa_provider_network_support import (
    CIDR,
    GATEWAY_ID,
    IP_SPACE_ID,
    LINK_ID,
    SETTING_ID,
    TASK_ID,
    TASK_LOCATION,
    V90,
    V91,
    card,
    gateway,
    ip_space,
    item,
    link,
    regional,
    set_target_version,
)
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TARGET_NAME,
    TENANT_ID,
    mount_logins,
    page,
    run,
    seed_target,
    wire_connector,
)

_OP = "vcfa.provider.network.delete"
_SETTING = item(V90, "regionalNetworkingSettings", SETTING_ID)
_GATEWAY = item(V90, "providerGateways", GATEWAY_ID)
_IP_SPACE = item(V90, "ipSpaces", IP_SPACE_ID)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _args(params: dict[str, Any]) -> dict[str, Any]:
    return {"connector_id": "vcfa-rest-9.0", "op_id": _OP, "target": TARGET_NAME, "params": params}


def _approver() -> Operator:
    return Operator(
        sub="vcfa-prov-approver",
        name="VCFA Approver",
        email=None,
        raw_jwt="<vcfa-approver-raw-jwt>",
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _blast(effect: dict[str, Any] | None) -> dict[str, Any]:
    assert effect is not None
    preview = effect.get("preview") or {}
    block = effect.get("blast_radius") or preview.get("blast_radius")
    assert isinstance(block, dict), effect
    return block


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


async def test_card_for_regional_networking_names_what_goes_with_it(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SETTING).respond(200, json=regional())
        effect = await card(_OP, {"kind": "regional_networking", "id": SETTING_ID}, vcfa)
    block = _blast(effect)
    assert block["object"]["id"] == SETTING_ID
    assert block["object"]["org_name"] == "example-org"
    assert [child["kind"] for child in block["children"]] == [
        "nsx_project",
        "transit_gateway",
        "default_vpc",
    ]
    assert block["irreversibility"] == "permanent"


async def test_card_for_a_gateway_lists_its_ip_space_links(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GATEWAY).respond(200, json=gateway())
        mock.get(f"{V90}/regionalNetworkingSettings").respond(200, json=page([]))
        mock.get(f"{V90}/ipSpaceAssociations").respond(200, json=page([link()]))
        effect = await card(_OP, {"kind": "provider_gateway", "id": GATEWAY_ID}, vcfa)
    block = _blast(effect)
    assert block["children"] == [
        {"kind": "ip_space_link", "id": LINK_ID, "ip_space": "example-ip-space"}
    ]


async def test_card_for_an_ip_space_lists_its_ranges(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_IP_SPACE).respond(200, json=ip_space())
        mock.get(f"{V90}/ipSpaceAssociations").respond(200, json=page([]))
        effect = await card(_OP, {"kind": "ip_space", "id": IP_SPACE_ID}, vcfa)
    assert _blast(effect)["children"] == [{"kind": "nsx_ip_block", "cidr": CIDR}]


async def test_card_refuses_an_object_that_does_not_exist(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SETTING).respond(404, json={"message": "not found"})
        effect = await card(_OP, {"kind": "regional_networking", "id": SETTING_ID}, vcfa)
    assert effect is not None
    assert "blast_radius" not in effect
    assert "no regional networking" in json.dumps(effect)


async def test_wrong_order_gateway_before_regional_networking_is_refused(
    vcfa: VcfAutomationConnector,
) -> None:
    args = _args({"kind": "provider_gateway", "id": GATEWAY_ID})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GATEWAY).respond(200, json=gateway())
        mock.get(f"{V90}/regionalNetworkingSettings").respond(200, json=page([regional()]))
        delete = mock.delete(_GATEWAY).respond(202, headers={"Location": TASK_LOCATION})
        preview = await preview_operation(OPERATOR, args)
        call = await call_operation(
            OPERATOR, {**args, "preview_hash": preview.get("preview_hash", "none")}
        )
        direct = await run(_OP, {"kind": "provider_gateway", "id": GATEWAY_ID})
    assert call["status"] == "denied", call
    assert "Remove the regional networking first" in json.dumps(call)
    assert direct["result"]["status"] == "precondition_failed"
    assert direct["result"]["blockers"][0]["id"] == SETTING_ID
    assert not delete.called


async def test_vendor_refuses_the_wrong_order(vcfa: VcfAutomationConnector) -> None:
    """MEHO saw no user, but the appliance still refuses: its error text comes back."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_GATEWAY).respond(200, json=gateway())
        mock.get(f"{V90}/regionalNetworkingSettings").respond(200, json=page([]))
        mock.delete(_GATEWAY).respond(
            400, json={"message": "Provider Gateway is in use by Regional Networking Setting."}
        )
        result = await run(_OP, {"kind": "provider_gateway", "id": GATEWAY_ID})
    assert result["status"] == "error", result
    assert "in use by Regional Networking Setting" in json.dumps(result)


# ---------------------------------------------------------------------------
# The approval gate and the run
# ---------------------------------------------------------------------------


async def test_delete_without_preview_hash_is_refused(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SETTING).respond(200, json=regional())
        delete = mock.delete(_SETTING).respond(202, headers={"Location": TASK_LOCATION})
        call = await call_operation(
            OPERATOR, _args({"kind": "regional_networking", "id": SETTING_ID})
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not delete.called


async def test_delete_waits_for_a_second_person_then_runs(vcfa: VcfAutomationConnector) -> None:
    args = _args({"kind": "regional_networking", "id": SETTING_ID})
    approver = _approver()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SETTING).respond(200, json=regional())
        delete = mock.delete(_SETTING).respond(202, headers={"Location": TASK_LOCATION})

        preview = await preview_operation(OPERATOR, args)
        assert preview["status"] == "ok", preview
        call = await call_operation(OPERATOR, {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        assert not delete.called
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            effect = dict(row.proposed_effect)
        assert effect["safety_level"] == "destructive"
        assert effect["blast_radius"]["object"]["id"] == SETTING_ID

        async with get_sessionmaker()() as session:
            with pytest.raises(SelfApprovalForbiddenError):
                await approve_request(session, request_id, operator=OPERATOR, params=None)

        async with get_sessionmaker()() as session:
            approved = await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        assert approved.status == "approved"

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed
    assert resumed.result["status"] == "accepted"
    assert resumed.result["job_id"] == TASK_ID
    assert resumed.result["status_read"]["params"] == {
        "kind": "regional_networking",
        "id": SETTING_ID,
        "job_id": TASK_ID,
    }
    assert delete.call_count == 1


async def test_approved_run_stops_when_the_object_changed(vcfa: VcfAutomationConnector) -> None:
    token = approved_preview_var.set((_OP, {"state_hash": "seen-before-a-change"}))
    try:
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
            mount_logins(mock)
            mock.get(_SETTING).respond(200, json=regional())
            delete = mock.delete(_SETTING).respond(202, headers={"Location": TASK_LOCATION})
            result = await run(_OP, {"kind": "regional_networking", "id": SETTING_ID})
    finally:
        approved_preview_var.reset(token)
    assert result["result"]["status"] == "precondition_failed"
    assert "changed after the approver saw it" in result["result"]["guidance"]
    assert not delete.called


async def test_delete_of_an_object_already_gone(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SETTING).respond(404)
        delete = mock.delete(_SETTING).respond(202)
        result = await run(_OP, {"kind": "regional_networking", "id": SETTING_ID})
    assert result["result"]["status"] == "already_absent"
    assert not delete.called


async def test_delete_while_a_job_runs_starts_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SETTING).respond(200, json=regional(status="CONFIGURING"))
        delete = mock.delete(_SETTING).respond(202)
        result = await run(_OP, {"kind": "regional_networking", "id": SETTING_ID})
    assert result["result"]["status"] == "already_running"
    assert not delete.called


async def test_delete_of_an_ip_space_link_on_9_1(vcfa: VcfAutomationConnector) -> None:
    await set_target_version("9.1")
    path = item(V91, "ipSpaceAssociations", LINK_ID)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(path).respond(200, json=link())
        delete = mock.delete(path).mock(
            return_value=httpx.Response(202, headers={"Location": TASK_LOCATION})
        )
        result = await run(_OP, {"kind": "ip_space_association", "id": LINK_ID})
    assert result["result"]["status"] == "accepted", result
    assert delete.call_count == 1


@pytest.mark.parametrize("bad_id", ["..", ".", "a/b"])
async def test_delete_refuses_an_id_that_is_not_an_id(
    vcfa: VcfAutomationConnector, bad_id: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.route(method="DELETE").respond(202)
        result = await run(_OP, {"kind": "ip_space", "id": bad_id})
    assert result["result"]["status"] == "invalid_request"
    assert not route.called
