# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA provider networking: the read and status read, IP spaces, gateways, regional networking.

Drives each op through the real dispatcher against a respx-mocked appliance.
Pins the risk levels; the 9.0 (``/cloudapi/vcf/``) and 9.1 (``/cloudapi/v1/``)
paths; the Tier-0 list filter; the job rules (``accepted`` with the vendor's
task id, a job that already runs starts nothing, ``not_reachable`` with the job
id and the time, a new login between start and status read, the steps of a
change that fails part-way); and the vendor errors the task names.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_PROVIDER_NETWORK_OPS,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation._provider_network import (
    provider_network_list,
    status_read,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VCFA_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.meta_tools import call_operation
from tests._vcfa_provider_network_support import (
    CIDR,
    GATEWAY_ID,
    IP_SPACE_ID,
    LINK_ID,
    ORG_ID,
    ORG_NETWORKING_PATH,
    ORG_PATH,
    REGION_ID,
    TASK_ID,
    TASK_LOCATION,
    TIER0_ID,
    TIER0_PATH,
    V90,
    V91,
    gateway,
    ip_space,
    item,
    link,
    org,
    regional,
    set_target_version,
    tier0,
)
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TARGET_NAME,
    mount_logins,
    page,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

#: The task table: op id -> (risk level, needs approval).
_RISK_TABLE = {
    "vcfa.provider.network.list": ("safe", False),
    "vcfa.provider.org.update": ("dangerous", True),
    "vcfa.provider.org.networking.set": ("dangerous", True),
    "vcfa.provider.ip_space.set": ("dangerous", True),
    "vcfa.provider.provider_gateway.set": ("dangerous", True),
    "vcfa.provider.regional_networking.set": ("dangerous", True),
    "vcfa.provider.network.delete": ("destructive", True),
}


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


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": "vcfa-rest-9.0",
        "op_id": op_id,
        "target": TARGET_NAME,
        "params": params,
    }


#: The legacy task read of the made-up job.
TASK = f"/api/task/{TASK_ID}"


def _accepted(location: str = TASK_LOCATION) -> httpx.Response:
    return httpx.Response(202, headers={"Location": location})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_risk_levels_match_the_task() -> None:
    assert {
        op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_PROVIDER_NETWORK_OPS
    } == _RISK_TABLE


def test_group_text_names_every_op() -> None:
    text = VCFA_TYPED_WHEN_TO_USE_BY_GROUP["vcfa-provider-networking"]
    for op_id in _RISK_TABLE:
        assert op_id in text


async def test_ops_register_as_typed_rows(vcfa: VcfAutomationConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(list(_RISK_TABLE)))
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    assert set(by_op) == set(_RISK_TABLE)
    for op_id, (level, approval) in _RISK_TABLE.items():
        assert by_op[op_id].source_kind == "typed"
        assert by_op[op_id].safety_level == level
        assert by_op[op_id].requires_approval is approval


# ---------------------------------------------------------------------------
# vcfa.provider.network.list
# ---------------------------------------------------------------------------


async def test_list_ip_spaces_of_a_region_on_9_0(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(f"{V90}/ipSpaces").respond(
            200, json=page([ip_space(), ip_space(status="CONFIGURING")])
        )
        result = await run(
            "vcfa.provider.network.list", {"kind": "ip_spaces", "region_id": REGION_ID}
        )
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["reachable"] is True
    assert out["state"] is None
    assert out["count"] == 2
    row = out["items"][0]
    assert row["cidrs"] == [CIDR]
    assert row["region_id"] == REGION_ID
    assert row["max_subnet_size"] == 24
    assert [r["job_state"] for r in out["items"]] == ["succeeded", "running"]
    assert listing.calls.last.request.url.params["filter"] == f"regionRef.id=={REGION_ID}"


async def test_list_uses_the_v1_path_on_9_1(vcfa: VcfAutomationConnector) -> None:
    await set_target_version("9.1.1")
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        old = mock.get(f"{V90}/providerGateways").respond(200, json=page([]))
        new = mock.get(f"{V91}/providerGateways").respond(200, json=page([gateway()]))
        result = await run("vcfa.provider.network.list", {"kind": "provider_gateways"})
    assert result["result"]["items"][0]["tier0_id"] == TIER0_ID
    assert new.called
    assert not old.called


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"region_id": REGION_ID}, f"regionId=={REGION_ID}"),
        (
            {"nsx_manager_id": "urn:vcloud:nsxtmanager:example"},
            "networkProviderId==urn:vcloud:nsxtmanager:example",
        ),
    ],
)
async def test_tier0_list_always_sends_a_filter(
    vcfa: VcfAutomationConnector, params: dict[str, Any], expected: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(TIER0_PATH).respond(200, json={"values": [tier0()]})
        result = await run("vcfa.provider.network.list", {"kind": "tier0_gateways", **params})
    assert result["result"]["items"][0]["already_imported"] is False
    assert listing.calls.last.request.url.params["filter"] == expected


async def test_tier0_list_without_a_filter_makes_no_call(vcfa: VcfAutomationConnector) -> None:
    """Without a filter the appliance answers an empty list, so MEHO never asks that way."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(TIER0_PATH).respond(200, json={"values": []})
        through_dispatch = await run("vcfa.provider.network.list", {"kind": "tier0_gateways"})
        direct = await provider_network_list(
            vcfa, OPERATOR, await resolved_target(), {"kind": "tier0_gateways"}
        )
    assert (
        through_dispatch["status"] != "ok"
        or through_dispatch["result"]["state"] == "invalid_request"
    )
    assert direct["state"] == "invalid_request"
    assert "region_id or nsx_manager_id" in direct["guidance"]
    assert not listing.called


async def test_org_networking_read(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(
            200, json={"networkingTenancyEnabled": True, "orgNameForLogs": "exorg"}
        )
        result = await run("vcfa.provider.network.list", {"kind": "org_networking", "org": ORG_ID})
    assert result["result"]["items"] == [
        {
            "org_id": ORG_ID,
            "org_name": "example-org",
            "is_enabled": True,
            "networking_enabled": True,
            "org_name_for_logs": "exorg",
        }
    ]


@pytest.mark.parametrize(
    ("vendor", "state"),
    [
        ("PENDING", "queued"),
        ("CONFIGURING", "running"),
        ("REALIZED", "succeeded"),
        ("REALIZATION_FAILED", "failed"),
    ],
)
async def test_status_read_of_one_object(
    vcfa: VcfAutomationConnector, vendor: str, state: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(
            200, json=gateway(status=vendor)
        )
        result = await run(
            "vcfa.provider.network.list", {"kind": "provider_gateways", "id": GATEWAY_ID}
        )
    out = result["result"]
    assert out["state"] == state
    assert out["vendor_status"] == vendor
    assert out["job"] is None
    if state == "failed":
        assert "job_id" in out["guidance"]


def _task(status: str, **extra: Any) -> dict[str, Any]:
    """A made-up vendor task, as the legacy task read answers it."""
    return {
        "id": f"urn:vcloud:task:{TASK_ID}",
        "status": status,
        "operationName": "providerGatewayCreate",
        "startTime": "2026-01-01T00:00:00.000Z",
        **extra,
    }


async def test_status_read_of_a_failed_job_gives_the_vendor_error(
    vcfa: VcfAutomationConnector,
) -> None:
    vendor_text = "Tier-0 gateway is already used by another provider gateway. " + "x" * 900
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(
            200, json=gateway(status="REALIZATION_FAILED")
        )
        task_read = mock.get(TASK).respond(
            200, json=_task("error", progress=100, error={"message": vendor_text})
        )
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "provider_gateways", "id": GATEWAY_ID, "job_id": TASK_ID},
        )
    out = result["result"]
    assert out["state"] == "failed"
    assert out["job_state"] == "failed"
    assert out["error"].startswith("Tier-0 gateway is already used")
    assert len(out["error"]) <= 500
    assert out["job"]["vendor_state"] == "error"
    assert out["job"]["read"] is True
    assert out["job_id"] == TASK_ID
    assert "vendor's text" in out["guidance"]
    # The legacy task read carries the classic Accept header.
    assert "version=40.0" in task_read.calls.last.request.headers["Accept"]


async def test_status_read_of_a_running_job_gives_its_progress(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(200, json=gateway())
        mock.get(TASK).respond(200, json=_task("running", progress=40))
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "provider_gateways", "id": GATEWAY_ID, "job_id": f"urn:vcloud:task:{TASK_ID}"},
        )
    out = result["result"]
    assert out["state"] == "running"
    assert out["job_state"] == "running"
    assert out["progress"] == 40
    assert out["vendor_status"] == "REALIZED"
    assert out["error"] is None


async def test_status_read_when_the_task_read_gets_503_is_not_reachable(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(200, json=gateway())
        mock.get(TASK).respond(503, text="upgrading")
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "provider_gateways", "id": GATEWAY_ID, "job_id": TASK_ID},
        )
    out = result["result"]
    assert out["reachable"] is False
    assert out["state"] == "not_reachable"
    assert out["job_id"] == TASK_ID
    assert out["checked_at"].endswith("Z")
    # The object rows read before the task read stay in the answer.
    assert out["count"] == 1
    assert [row["id"] for row in out["items"]] == [GATEWAY_ID]


async def test_status_read_after_a_finished_delete_is_not_found(
    vcfa: VcfAutomationConnector,
) -> None:
    """The object is gone and the task succeeded: the delete's status read ends."""
    params = status_read("ip_spaces", id=IP_SPACE_ID, job_id=TASK_ID)["params"]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "ipSpaces", IP_SPACE_ID)).respond(404, json={"message": "gone"})
        mock.get(TASK).respond(200, json=_task("success", progress=100))
        result = await run("vcfa.provider.network.list", params)
    out = result["result"]
    assert out["state"] == "not_found"
    assert out["job_state"] == "succeeded"
    assert out["job"]["read"] is True


@pytest.mark.parametrize("task_status", ["running", "error"])
async def test_status_read_of_a_missing_object_keeps_a_running_or_failed_job_state(
    vcfa: VcfAutomationConnector, task_status: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "ipSpaces", IP_SPACE_ID)).respond(404, json={"message": "gone"})
        mock.get(TASK).respond(200, json=_task(task_status))
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "ip_spaces", "id": IP_SPACE_ID, "job_id": TASK_ID},
        )
    expected = {"running": "running", "error": "failed"}[task_status]
    assert result["result"]["state"] == expected


@pytest.mark.parametrize("code", [400, 403, 406])
async def test_status_read_when_the_task_read_is_refused_keeps_the_object_state(
    vcfa: VcfAutomationConnector, code: int
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(200, json=gateway())
        mock.get(TASK).respond(code, json={"message": "no"})
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "provider_gateways", "id": GATEWAY_ID, "job_id": TASK_ID},
        )
    out = result["result"]
    assert out["reachable"] is True
    assert out["state"] == "succeeded"
    assert out["job"]["read"] is False
    assert f"HTTP {code}" in out["job"]["why_not_read"]
    assert out["count"] == 1


async def test_status_read_of_a_job_the_vendor_no_longer_has(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "ipSpaces", IP_SPACE_ID)).respond(404, json={"message": "gone"})
        mock.get(TASK).respond(404, json={"message": "no task"})
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "ip_spaces", "id": IP_SPACE_ID, "job_id": TASK_ID},
        )
    out = result["result"]
    assert out["state"] == "not_found"
    assert out["job_state"] == "unknown"
    assert out["job"]["read"] is False
    assert "could not read the vendor job" in out["guidance"]


@pytest.mark.parametrize(
    "job_id", ["..", ".", "urn:vcloud:task:", "a/b", "foo:bar", f"urn:vcloud:task:x:{TASK_ID}"]
)
async def test_status_read_refuses_a_bad_job_id(vcfa: VcfAutomationConnector, job_id: str) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(200, json=gateway())
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "provider_gateways", "id": GATEWAY_ID, "job_id": job_id},
        )
    assert result["result"]["state"] == "invalid_request"
    assert read.call_count == 0


async def test_status_read_of_a_deleted_object_is_not_found(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "ipSpaces", IP_SPACE_ID)).respond(404, json={"message": "not found"})
        result = await run("vcfa.provider.network.list", {"kind": "ip_spaces", "id": IP_SPACE_ID})
    assert result["result"]["state"] == "not_found"


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("connection refused"), httpx.Response(503, text="upgrading")],
    ids=["connect-error", "http-503"],
)
async def test_status_read_when_the_appliance_does_not_answer(
    vcfa: VcfAutomationConnector, answer: Any
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(item(V90, "providerGateways", GATEWAY_ID))
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "provider_gateways", "id": GATEWAY_ID, "job_id": TASK_ID},
        )
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["reachable"] is False
    assert out["state"] == "not_reachable"
    assert out["job_id"] == TASK_ID
    assert out["checked_at"].endswith("Z")
    assert "failed" not in out["state"]


async def test_status_read_logs_in_again_after_the_login_expired(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(item(V90, "providerGateways", GATEWAY_ID)).mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json=gateway())]
        )
        result = await run(
            "vcfa.provider.network.list", {"kind": "provider_gateways", "id": GATEWAY_ID}
        )
        logins = mock.routes[0].call_count
    assert result["result"]["state"] == "succeeded"
    assert logins == 2


# ---------------------------------------------------------------------------
# vcfa.provider.ip_space.set
# ---------------------------------------------------------------------------

_IP_SPACE_PARAMS: dict[str, Any] = {
    "name": "example-ip-space",
    "region_id": REGION_ID,
    "cidr_blocks": [{"cidr": CIDR}],
    "default_quota": {"max_cidr_count": 5, "max_ip_count": 5, "max_subnet_size": 24},
}


async def test_ip_space_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaces").respond(200, json=page([]))
        post = mock.post(f"{V90}/ipSpaces").mock(return_value=_accepted())
        call = await call_operation(
            OPERATOR,
            _args("vcfa.provider.ip_space.set", {**_IP_SPACE_PARAMS, "range_is_new_in_nsx": True}),
        )
    assert call["status"] == "awaiting_approval", call
    assert not post.called


async def test_ip_space_create_needs_the_person_to_confirm_the_range_is_new(
    vcfa: VcfAutomationConnector,
) -> None:
    """Two NSX blocks with the same range would hand out the same addresses."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaces").respond(200, json=page([]))
        post = mock.post(f"{V90}/ipSpaces").mock(return_value=_accepted())
        result = await run("vcfa.provider.ip_space.set", dict(_IP_SPACE_PARAMS))
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert "range_is_new_in_nsx" in out["guidance"]
    assert "same addresses" in out["guidance"]
    assert not post.called


async def test_ip_space_create_answers_accepted_with_the_task_id(
    vcfa: VcfAutomationConnector,
) -> None:
    """The create answers 202 with an empty body: MEHO says accepted, not done."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaces").respond(200, json=page([]))
        post = mock.post(f"{V90}/ipSpaces").mock(return_value=_accepted())
        result = await run(
            "vcfa.provider.ip_space.set", {**_IP_SPACE_PARAMS, "range_is_new_in_nsx": True}
        )
    out = result["result"]
    assert out["status"] == "accepted"
    assert out["operation"] == "create"
    assert out["job_id"] == TASK_ID
    assert out["status_read"] == {
        "op_id": "vcfa.provider.network.list",
        "params": {
            "kind": "ip_spaces",
            "name": "example-ip-space",
            "region_id": REGION_ID,
            "job_id": TASK_ID,
        },
    }
    assert "not done" in out["guidance"]
    body = json.loads(post.calls.last.request.content)
    assert body["regionRef"] == {"id": REGION_ID}
    assert body["internalScopeCidrBlocks"] == [{"cidr": CIDR}]
    assert body["defaultQuota"] == {"maxCidrCount": 5, "maxIpCount": 5, "maxSubnetSize": 24}


async def test_ip_space_set_while_a_job_runs_starts_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaces").respond(200, json=page([ip_space(status="PENDING")]))
        post = mock.post(f"{V90}/ipSpaces").mock(return_value=_accepted())
        put = mock.put(item(V90, "ipSpaces", IP_SPACE_ID)).mock(return_value=_accepted())
        result = await run(
            "vcfa.provider.ip_space.set",
            {
                **_IP_SPACE_PARAMS,
                "cidr_blocks": [{"cidr": "198.51.100.0/24"}],
                "range_is_new_in_nsx": True,
            },
        )
    out = result["result"]
    assert out["status"] == "already_running"
    assert out["object"]["id"] == IP_SPACE_ID
    assert not post.called
    assert not put.called


async def test_ip_space_change_adds_a_range_on_the_9_1_path(vcfa: VcfAutomationConnector) -> None:
    await set_target_version("9.1")
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V91}/ipSpaces").respond(200, json=page([ip_space()]))
        put = mock.put(item(V91, "ipSpaces", IP_SPACE_ID)).mock(return_value=_accepted())
        result = await run(
            "vcfa.provider.ip_space.set",
            {
                "name": "example-ip-space",
                "region_id": REGION_ID,
                "cidr_blocks": [{"cidr": CIDR}, {"cidr": "198.51.100.0/24"}],
                "range_is_new_in_nsx": True,
            },
        )
    assert result["result"]["status"] == "accepted", result
    assert result["result"]["operation"] == "update"
    body = json.loads(put.calls.last.request.content)
    assert [b["cidr"] for b in body["internalScopeCidrBlocks"]] == [CIDR, "198.51.100.0/24"]
    assert "status" not in body


async def test_ip_space_set_unchanged_writes_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaces").respond(200, json=page([ip_space()]))
        put = mock.put(item(V90, "ipSpaces", IP_SPACE_ID)).mock(return_value=_accepted())
        result = await run("vcfa.provider.ip_space.set", dict(_IP_SPACE_PARAMS))
    assert result["result"]["status"] == "unchanged"
    assert not put.called


async def test_ip_space_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaces").respond(200, json=page([]))
        mock.post(f"{V90}/ipSpaces").respond(
            400, json={"message": "IP Block overlaps with an existing IP Block."}
        )
        result = await run(
            "vcfa.provider.ip_space.set", {**_IP_SPACE_PARAMS, "range_is_new_in_nsx": True}
        )
    assert result["status"] == "error", result
    assert "overlaps with an existing IP Block" in json.dumps(result)


# ---------------------------------------------------------------------------
# vcfa.provider.provider_gateway.set
# ---------------------------------------------------------------------------

_GATEWAY_PARAMS: dict[str, Any] = {
    "name": "example-gateway",
    "region_id": REGION_ID,
    "tier0": "example-tier0",
    "ip_space_ids": [IP_SPACE_ID],
}


async def test_gateway_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(f"{V90}/providerGateways").mock(return_value=_accepted())
        call = await call_operation(
            OPERATOR, _args("vcfa.provider.provider_gateway.set", dict(_GATEWAY_PARAMS))
        )
    assert call["status"] == "awaiting_approval", call
    assert not post.called


async def test_gateway_create_on_9_0(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/providerGateways").respond(200, json=page([]))
        tier0_list = mock.get(TIER0_PATH).respond(200, json={"values": [tier0()]})
        post = mock.post(f"{V90}/providerGateways").mock(return_value=_accepted())
        result = await run("vcfa.provider.provider_gateway.set", dict(_GATEWAY_PARAMS))
    out = result["result"]
    assert out["status"] == "accepted", out
    assert out["job_id"] == TASK_ID
    assert out["steps"] == [{"step": "create_gateway", "status": "accepted", "job_id": TASK_ID}]
    assert tier0_list.calls.last.request.url.params["filter"] == f"regionId=={REGION_ID}"
    body = json.loads(post.calls.last.request.content)
    assert body == {
        "name": "example-gateway",
        "regionRef": {"id": REGION_ID},
        "backingType": "NSX_TIER0",
        "backingRef": {"id": TIER0_ID},
        "ipSpaceRefs": [{"id": IP_SPACE_ID}],
    }


async def test_gateway_on_9_0_refuses_fields_of_newer_builds(vcfa: VcfAutomationConnector) -> None:
    """9.0 answers 400 Unrecognized field for them, so MEHO stops before any call."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(f"{V90}/providerGateways").respond(200, json=page([]))
        post = mock.post(f"{V90}/providerGateways").mock(return_value=_accepted())
        result = await run(
            "vcfa.provider.provider_gateway.set",
            {**_GATEWAY_PARAMS, "nat": {"enable_snat": True, "ip_space_id": IP_SPACE_ID}},
        )
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert "Unrecognized field" in out["guidance"]
    assert not listing.called
    assert not post.called


async def test_gateway_create_on_9_1_sends_the_newer_fields(vcfa: VcfAutomationConnector) -> None:
    await set_target_version("9.1")
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V91}/providerGateways").respond(200, json=page([]))
        mock.get(TIER0_PATH).respond(200, json={"values": [tier0(parent="parent-t0")]})
        post = mock.post(f"{V91}/providerGateways").mock(return_value=_accepted())
        result = await run(
            "vcfa.provider.provider_gateway.set",
            {**_GATEWAY_PARAMS, "inbound_remote_networks": ["0.0.0.0/0"]},
        )
    assert result["result"]["status"] == "accepted", result
    body = json.loads(post.calls.last.request.content)
    assert body["backingType"] == "NSX_VRF_TIER0"
    assert body["inboundRemoteNetworks"] == ["0.0.0.0/0"]


async def test_gateway_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/providerGateways").respond(200, json=page([]))
        mock.get(TIER0_PATH).respond(200, json={"values": [tier0()]})
        mock.post(f"{V90}/providerGateways").respond(
            400, json={"message": 'Unrecognized field "natConfig"'}
        )
        result = await run("vcfa.provider.provider_gateway.set", dict(_GATEWAY_PARAMS))
    assert result["status"] == "error", result
    assert "Unrecognized field" in json.dumps(result)


async def test_gateway_create_refuses_a_used_tier0(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/providerGateways").respond(200, json=page([]))
        mock.get(TIER0_PATH).respond(200, json={"values": [tier0(imported=True)]})
        post = mock.post(f"{V90}/providerGateways").mock(return_value=_accepted())
        result = await run("vcfa.provider.provider_gateway.set", dict(_GATEWAY_PARAMS))
    assert result["result"]["status"] == "invalid_request"
    assert not post.called


async def test_gateway_links_fail_part_way(vcfa: VcfAutomationConnector) -> None:
    """Step two fails: the answer lists what ran, what failed and what did not run."""
    second, third = IP_SPACE_ID + "-2", IP_SPACE_ID + "-3"
    fourth = IP_SPACE_ID + "-4"
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/providerGateways").respond(200, json=page([gateway()]))
        mock.get(f"{V90}/ipSpaceAssociations").respond(200, json=page([link()]))
        post = mock.post(f"{V90}/ipSpaceAssociations").mock(
            side_effect=[
                _accepted(),
                httpx.Response(400, json={"message": "IP Space is in another region."}),
            ]
        )
        result = await run(
            "vcfa.provider.provider_gateway.set",
            {**_GATEWAY_PARAMS, "ip_space_ids": [IP_SPACE_ID, second, third, fourth]},
        )
    out = result["result"]
    assert out["status"] == "failed_part_way", out
    assert out["steps"] == [
        {"step": f"link_ip_space:{second}", "status": "accepted", "job_id": TASK_ID}
    ]
    assert out["step_failed"]["step"] == f"link_ip_space:{third}"
    assert out["step_failed"]["http_status"] == 400
    assert "another region" in out["step_failed"]["error"]
    assert out["steps_not_run"] == [f"link_ip_space:{fourth}"]
    assert post.call_count == 2
    first_body = json.loads(post.calls[0].request.content)
    assert first_body == {
        "ipSpaceRef": {"id": second},
        "providerGatewayRef": {"id": GATEWAY_ID},
    }


async def test_gateway_with_every_link_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/providerGateways").respond(200, json=page([gateway()]))
        links = mock.get(f"{V90}/ipSpaceAssociations").respond(200, json=page([link()]))
        post = mock.post(f"{V90}/ipSpaceAssociations").mock(return_value=_accepted())
        result = await run("vcfa.provider.provider_gateway.set", dict(_GATEWAY_PARAMS))
    assert result["result"]["status"] == "unchanged"
    assert links.calls.last.request.url.params["filter"] == f"providerGatewayRef.id=={GATEWAY_ID}"
    assert not post.called


# ---------------------------------------------------------------------------
# vcfa.provider.regional_networking.set
# ---------------------------------------------------------------------------

_REGIONAL_PARAMS: dict[str, Any] = {
    "org": ORG_ID,
    "region_id": REGION_ID,
    "provider_gateway_id": GATEWAY_ID,
}


def _mount_regional_reads(
    mock: respx.MockRouter, *, short_name: str = "exorg", gateway_status: str = "REALIZED"
) -> None:
    mock.get(ORG_PATH).respond(200, json=org())
    mock.get(f"{V90}/regionalNetworkingSettings").respond(200, json=page([]))
    mock.get(ORG_NETWORKING_PATH).respond(
        200, json={"networkingTenancyEnabled": True, "orgNameForLogs": short_name}
    )
    mock.get(item(V90, "providerGateways", GATEWAY_ID)).respond(
        200, json=gateway(status=gateway_status)
    )


async def test_regional_networking_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_regional_reads(mock)
        post = mock.post(f"{V90}/regionalNetworkingSettings").mock(return_value=_accepted())
        call = await call_operation(
            OPERATOR, _args("vcfa.provider.regional_networking.set", dict(_REGIONAL_PARAMS))
        )
    assert call["status"] == "awaiting_approval", call
    assert not post.called


async def test_regional_networking_before_the_short_log_name_is_set(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_regional_reads(mock, short_name="")
        post = mock.post(f"{V90}/regionalNetworkingSettings").mock(return_value=_accepted())
        result = await run("vcfa.provider.regional_networking.set", dict(_REGIONAL_PARAMS))
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert "Short Log Name is not set" in out["guidance"]
    assert "vcfa.provider.org.networking.set" in out["guidance"]
    assert not post.called


async def test_regional_networking_vendor_400_short_log_name(
    vcfa: VcfAutomationConnector,
) -> None:
    """When the appliance itself refuses, its error text reaches the answer."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_regional_reads(mock)
        mock.post(f"{V90}/regionalNetworkingSettings").respond(
            400, json={"message": "Short Log Name is not set for Organization example-org."}
        )
        result = await run("vcfa.provider.regional_networking.set", dict(_REGIONAL_PARAMS))
    assert result["status"] == "error", result
    assert "Short Log Name is not set" in json.dumps(result)


async def test_regional_networking_waits_for_a_realized_gateway(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_regional_reads(mock, gateway_status="CONFIGURING")
        post = mock.post(f"{V90}/regionalNetworkingSettings").mock(return_value=_accepted())
        result = await run("vcfa.provider.regional_networking.set", dict(_REGIONAL_PARAMS))
    assert result["result"]["status"] == "invalid_request"
    assert "REALIZED" in result["result"]["guidance"]
    assert not post.called


async def test_regional_networking_accepted(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_regional_reads(mock)
        post = mock.post(f"{V90}/regionalNetworkingSettings").mock(return_value=_accepted())
        result = await run(
            "vcfa.provider.regional_networking.set",
            {**_REGIONAL_PARAMS, "edge_cluster_id": "urn:vcloud:edgeCluster:example"},
        )
    out = result["result"]
    assert out["status"] == "accepted", out
    assert out["job_id"] == TASK_ID
    assert out["status_read"]["params"] == {
        "kind": "regional_networking",
        "org_id": ORG_ID,
        "region_id": REGION_ID,
        "job_id": TASK_ID,
    }
    assert json.loads(post.calls.last.request.content) == {
        "orgRef": {"id": ORG_ID},
        "regionRef": {"id": REGION_ID},
        "providerGatewayRef": {"id": GATEWAY_ID},
        "serviceEdgeClusterRef": {"id": "urn:vcloud:edgeCluster:example"},
    }


async def test_regional_networking_that_exists_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        listing = mock.get(f"{V90}/regionalNetworkingSettings").respond(
            200, json=page([regional()])
        )
        post = mock.post(f"{V90}/regionalNetworkingSettings").mock(return_value=_accepted())
        result = await run("vcfa.provider.regional_networking.set", dict(_REGIONAL_PARAMS))
    assert result["result"]["status"] == "unchanged"
    assert listing.calls.last.request.url.params["filter"] == (
        f"regionRef.id=={REGION_ID};orgRef.id=={ORG_ID}"
    )
    assert not post.called


async def test_link_row_names_both_sides(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/ipSpaceAssociations").respond(200, json=page([link()]))
        result = await run(
            "vcfa.provider.network.list",
            {"kind": "ip_space_associations", "provider_gateway_id": GATEWAY_ID},
        )
    row = result["result"]["items"][0]
    assert row["id"] == LINK_ID
    assert row["ip_space_id"] == IP_SPACE_ID
    assert row["provider_gateway_id"] == GATEWAY_ID


async def test_new_gateway_without_a_tier0_is_invalid_request(vcfa: VcfAutomationConnector) -> None:
    params = {key: value for key, value in _GATEWAY_PARAMS.items() if key != "tier0"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{V90}/providerGateways").respond(200, json=page([]))
        post = mock.post(f"{V90}/providerGateways").mock(return_value=_accepted())
        result = await run("vcfa.provider.provider_gateway.set", params)
    assert result["result"]["status"] == "invalid_request"
    assert "tier0" in result["result"]["guidance"]
    assert not post.called
