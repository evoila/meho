# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Fleet request, environment, inventory sync, health and datacenter ops.

The seven typed ops of ``vcf_fleet/lifecycle.py`` (Aria Suite Lifecycle 8.x
and VCF 9.0 fleet management) against a mocked appliance (``respx``). The
saved answers are written by hand from the vendor API reference, with
made-up names and ids.

* The risk of each op matches the table of the Task: reads are ``safe``,
  the two changes wait for approval.
* Each change parks for approval through the dispatcher, then works on
  the approved path, and a vendor error reaches the answer.
* The vendor-job rules: a sync that already runs is not started again; a
  failed request returns the vendor's error causes; a ``401`` between the
  start and the status read logs in again; an appliance that does not
  answer gives ``not_reachable`` with the request id and the time; a
  retry that fails at any step lists what ran, what failed and what did
  not run.
* No secret reaches an answer, also not inside the vendor's error text.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_fleet import (
    FLEET_CONNECTOR_ID,
    FLEET_TYPED_OPS,
    FLEET_TYPED_WHEN_TO_USE_BY_GROUP,
    VcfFleetConnector,
)
from meho_backplane.connectors.vcf_fleet.lifecycle import REDACTED
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_HOST = "fleet-lifecycle.test.invalid"
_BASE_URL = f"https://{_HOST}"

_REQUESTS = "/lcm/request/api/v2/requests"
_REQUEST_ID = "req-0001"
_REQUEST = f"{_REQUESTS}/{_REQUEST_ID}"
_ENV_ID = "env-0001"
_PRODUCT_ID = "vrops"
_SYNC = f"/lcm/lcops/api/v2/environments/{_ENV_ID}/products/{_PRODUCT_ID}/inventory-sync"
_SECRET = "<secret-value>"

#: The risk of each op, from the table of the Task.
_RISK: dict[str, tuple[str, bool]] = {
    "fleet.request.list": ("safe", False),
    "fleet.request.get": ("safe", False),
    "fleet.request.retry": ("dangerous", True),
    "fleet.environment.get": ("safe", False),
    "fleet.product.inventory_sync": ("caution", True),
    "fleet.health": ("safe", False),
    "fleet.datacenter.list": ("safe", False),
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="fleet", version="9.0", impl_id="fleet-rest", cls=VcfFleetConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _Target:
    def __init__(self) -> None:
        self.product = "fleet"
        self.fingerprint = type("_FP", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
        self.name = "fleet-lifecycle"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/fleet/fleet-lifecycle"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-fleet-lifecycle",
        name="Fleet Lifecycle Operator",
        email=None,
        raw_jwt="op.fleet.lifecycle.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000ab"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Loader:
    """Stub credentials loader; counts the reads so a new login shows."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _target: object, _operator: Operator) -> dict[str, str]:
        self.calls += 1
        return {"username": "<user>", "password": f"<password-{self.calls}>"}


async def _bind(loader: _Loader | None = None) -> VcfFleetConnector:
    await VcfFleetConnector.register_operations()
    instance = get_or_create_connector_instance(VcfFleetConnector)
    assert isinstance(instance, VcfFleetConnector)
    instance._creds = type(instance._creds)(loader or _Loader(), product_label="fleet")
    return instance


async def _dispatch(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=FLEET_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=params,
    )


def _request(vmid: str, state: str, updated: int, **extra: Any) -> dict[str, Any]:
    return {
        "vmid": vmid,
        "requestName": f"name-{vmid}",
        "requestType": extra.pop("requestType", "PRODUCT_DEPLOY"),
        "requestReason": "made-up reason",
        "state": state,
        "executionStatus": state,
        "errorCause": None,
        "createdBy": "<user>",
        "lastUpdatedOn": updated,
        **extra,
    }


_CAUSE: dict[str, Any] = {
    "eventId": "evt-0001",
    "messageId": "LCM-MSG-0001",
    "message": "Check the node address and the admin password.",
    "exceptionMessage": "Failed to reach the node.",
    "exceptionStackTrace": "<stack>",
    "retry": False,
    "properties": {
        "nodeAddress": "<node-old>",
        "adminPassword": "locker:password:<id>:<alias>",
    },
}


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


def test_risk_of_each_op_matches_the_table() -> None:
    ops = {op.op_id: op for op in FLEET_TYPED_OPS}
    for op_id, (safety, approval) in _RISK.items():
        assert ops[op_id].safety_level == safety, op_id
        assert ops[op_id].requires_approval is approval, op_id
        assert ("write" in ops[op_id].tags) is approval, op_id
        if approval:
            assert ops[op_id].llm_instructions is not None
            assert "preconditions" in ops[op_id].llm_instructions, op_id


def test_group_texts_name_the_reads_and_changes() -> None:
    ops = {op.op_id: op for op in FLEET_TYPED_OPS}
    for op_id in _RISK:
        group = ops[op_id].group_key
        assert group is not None
        assert op_id in FLEET_TYPED_WHEN_TO_USE_BY_GROUP[group], op_id


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_list_filters_sorts_and_limits(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    payload = [
        _request("req-a", "COMPLETED", 1),
        _request(
            "req-b",
            "FAILED",
            3,
            inputMap={"environmentId": _ENV_ID, "productId": _PRODUCT_ID, "password": "s3cret"},
        ),
        _request("req-c", "FAILED", 2),
        _request("req-d", "INPROGRESS", 4),
    ]
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUESTS).respond(200, json=payload)
            result = await _dispatch("fleet.request.list", {"outcome": "failed", "limit": 1})
            no_inputs = await _dispatch("fleet.request.list", {"outcome": "succeeded"})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    assert result.result["more_rows"] is True
    rows = result.result["requests"]
    assert [row["request_id"] for row in rows] == ["req-b"]
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["environment_id"] == _ENV_ID
    assert rows[0]["product_id"] == _PRODUCT_ID
    assert "inputMap" not in rows[0]
    assert "s3cret" not in json.dumps(rows[0])
    assert no_inputs.status == "ok", no_inputs.error
    row_a = no_inputs.result["requests"][0]
    assert (row_a["environment_id"], row_a["product_id"]) == (None, None)


@pytest.mark.asyncio
async def test_request_get_returns_the_failed_step_with_secrets_replaced(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "FAILED", 5))
            mock.get(f"{_REQUEST}/error-causes").respond(200, json=[_CAUSE])
            result = await _dispatch("fleet.request.get", {"request_id": _REQUEST_ID})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    answer = result.result
    assert answer["outcome"] == "failed"
    assert answer["failed_step"]["message"] == _CAUSE["message"]
    assert answer["failed_step"]["exception_message"] == _CAUSE["exceptionMessage"]
    assert answer["failed_step"]["properties"]["adminPassword"] == REDACTED
    assert "locker:password" not in json.dumps(answer)
    assert "<stack>" not in json.dumps(answer)


@pytest.mark.asyncio
async def test_request_get_logs_in_again_after_a_401(
    _stub_embedding: None, session: AsyncSession
) -> None:
    loader = _Loader()
    instance = await _bind(loader)
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            route = mock.get(_REQUEST)
            route.side_effect = [
                httpx.Response(401),
                httpx.Response(200, json=_request(_REQUEST_ID, "INPROGRESS", 5)),
            ]
            result = await _dispatch("fleet.request.get", {"request_id": _REQUEST_ID})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    assert result.result["outcome"] == "running"
    assert loader.calls == 2
    first, second = (call.request.headers["authorization"] for call in route.calls)
    assert first != second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("connection refused"), httpx.Response(503)],
    ids=["no-connection", "proxy-503"],
)
async def test_request_get_says_not_reachable_when_the_appliance_does_not_answer(
    _stub_embedding: None, session: AsyncSession, answer: Any
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            route = mock.get(_REQUEST)
            if isinstance(answer, httpx.Response):
                route.respond(answer.status_code)
            else:
                route.side_effect = answer
            result = await _dispatch("fleet.request.get", {"request_id": _REQUEST_ID})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    assert result.result["outcome"] == "not_reachable"
    assert result.result["request_id"] == _REQUEST_ID
    assert result.result["checked_at"]
    assert result.result["state"] is None


@pytest.mark.asyncio
async def test_request_get_says_not_reachable_when_the_error_causes_read_fails(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "FAILED", 5))
            mock.get(f"{_REQUEST}/error-causes").respond(503)
            result = await _dispatch("fleet.request.get", {"request_id": _REQUEST_ID})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    answer = result.result
    assert answer["outcome"] == "not_reachable"
    assert answer["request_id"] == _REQUEST_ID
    assert answer["state"] == "FAILED"
    assert answer["checked_at"]
    assert "HTTP 503" in answer["error"]
    assert answer["error_causes"] == []
    assert answer["failed_step"] is None


@pytest.mark.asyncio
async def test_request_get_replaces_a_secret_inside_vendor_text(
    _stub_embedding: None, session: AsyncSession
) -> None:
    request = _request(
        _REQUEST_ID,
        "FAILED",
        5,
        requestReason=f'retry with {{"adminPassword": "{_SECRET}"}}',
        errorCause=f'node refused {{"adminPassword":"{_SECRET}"}}',
    )
    cause = {
        **_CAUSE,
        "message": f'Login failed for {{"adminPassword": "{_SECRET}"}}',
        "exceptionMessage": f'Bad input: \\"rootPassword\\": \\"{_SECRET}\\"',
    }
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=request)
            mock.get(f"{_REQUEST}/error-causes").respond(200, json=[cause])
            result = await _dispatch("fleet.request.get", {"request_id": _REQUEST_ID})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    answer = result.result
    assert _SECRET not in json.dumps(answer)
    assert REDACTED in answer["reason"]
    assert REDACTED in answer["error_cause"]
    assert REDACTED in answer["failed_step"]["message"]
    assert REDACTED in answer["failed_step"]["exception_message"]
    assert answer["failed_step"]["event_id"] == "evt-0001"


@pytest.mark.asyncio
async def test_request_retry_waits_for_approval(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            retry = mock.patch(f"{_REQUEST}/retry").respond(200)
            result = await _dispatch("fleet.request.retry", {"request_id": _REQUEST_ID})
    finally:
        await instance.aclose()
    assert result.status == "awaiting_approval", result
    assert not retry.called


@pytest.mark.asyncio
async def test_request_retry_sends_the_causes_back_with_the_corrected_input(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "FAILED", 5))
            mock.get(f"{_REQUEST}/error-causes").respond(200, json=[_CAUSE])
            retry = mock.patch(f"{_REQUEST}/retry").respond(200)
            answer = await instance.request_retry(
                _operator(),
                _Target(),
                {"request_id": _REQUEST_ID, "properties": {"nodeAddress": "<node-new>"}},
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "accepted"
    assert answer["status_read"] == "fleet.request.get"
    assert answer["retried_event_ids"] == ["evt-0001"]
    assert [step["state"] for step in answer["steps"]] == ["done", "done", "done"]
    body = json.loads(retry.calls[0].request.content)
    assert body[0]["retry"] is True
    assert body[0]["eventId"] == "evt-0001"
    assert body[0]["properties"]["nodeAddress"] == "<node-new>"
    assert body[0]["properties"]["adminPassword"] == _CAUSE["properties"]["adminPassword"]


@pytest.mark.asyncio
async def test_request_retry_failing_at_the_last_step_lists_the_steps(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "FAILED", 5))
            mock.get(f"{_REQUEST}/error-causes").respond(200, json=[_CAUSE])
            mock.patch(f"{_REQUEST}/retry").respond(
                400, json={"errorCode": "LCM-ERR-0042", "message": "Retry is not allowed now."}
            )
            answer = await instance.request_retry(
                _operator(), _Target(), {"request_id": _REQUEST_ID}
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "failed"
    assert "Retry is not allowed now." in answer["reason"]
    states = {step["step"]: step["state"] for step in answer["steps"]}
    assert states == {"read_request": "done", "read_error_causes": "done", "retry": "failed"}
    assert "LCM-ERR-0042" in answer["steps"][2]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_path", "expected"),
    [
        (_REQUEST, {"read_request": "failed", "read_error_causes": "not_run", "retry": "not_run"}),
        (
            f"{_REQUEST}/error-causes",
            {"read_request": "done", "read_error_causes": "failed", "retry": "not_run"},
        ),
    ],
    ids=["first-step", "second-step"],
)
async def test_request_retry_failing_at_an_earlier_step_lists_the_steps(
    _stub_embedding: None, session: AsyncSession, failing_path: str, expected: dict[str, str]
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "FAILED", 5))
            mock.get(f"{_REQUEST}/error-causes").respond(200, json=[_CAUSE])
            mock.get(failing_path).respond(
                500, json={"errorCode": "LCM-ERR-0500", "message": "Internal error."}
            )
            retry = mock.patch(f"{_REQUEST}/retry").respond(200)
            answer = await instance.request_retry(
                _operator(), _Target(), {"request_id": _REQUEST_ID}
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "failed"
    assert "LCM-ERR-0500" in answer["reason"]
    assert answer["request_id"] == _REQUEST_ID
    states = {step["step"]: step["state"] for step in answer["steps"]}
    assert states == expected
    failed = [step for step in answer["steps"] if step["state"] == "failed"]
    assert "Internal error." in failed[0]["error"]
    assert not retry.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_body",
    [
        {"errorCode": "LCM-ERR-0042", "message": f'Bad input "adminPassword":"{_SECRET}"'},
        {"adminPassword": _SECRET, "detail": "no message field"},
    ],
    ids=["in-the-message", "in-the-raw-body"],
)
async def test_request_retry_vendor_error_text_has_secrets_replaced(
    _stub_embedding: None, session: AsyncSession, error_body: dict[str, Any]
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "FAILED", 5))
            mock.get(f"{_REQUEST}/error-causes").respond(200, json=[_CAUSE])
            mock.patch(f"{_REQUEST}/retry").respond(400, json=error_body)
            answer = await instance.request_retry(
                _operator(), _Target(), {"request_id": _REQUEST_ID}
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "failed"
    assert _SECRET not in json.dumps(answer)
    assert REDACTED in answer["reason"]
    assert REDACTED in answer["steps"][2]["error"]


@pytest.mark.asyncio
async def test_request_retry_starts_nothing_for_a_request_that_did_not_fail(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUEST).respond(200, json=_request(_REQUEST_ID, "INPROGRESS", 5))
            retry = mock.patch(f"{_REQUEST}/retry").respond(200)
            answer = await instance.request_retry(
                _operator(), _Target(), {"request_id": _REQUEST_ID}
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "not_started"
    assert answer["outcome"] == "running"
    assert [step["state"] for step in answer["steps"]] == ["done", "not_run", "not_run"]
    assert not retry.called


@pytest.mark.asyncio
async def test_request_retry_refuses_a_secret_value(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        with pytest.raises(ValueError, match="locker reference"):
            await instance.request_retry(
                _operator(),
                _Target(),
                {"request_id": _REQUEST_ID, "properties": {"adminPassword": "<clear-text>"}},
            )
    finally:
        await instance.aclose()


# ---------------------------------------------------------------------------
# Inventory sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inventory_sync_waits_for_approval(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            sync = mock.post(_SYNC).respond(200, json={"requestId": "req-sync"})
            result = await _dispatch(
                "fleet.product.inventory_sync",
                {"environment_id": _ENV_ID, "product_id": _PRODUCT_ID},
            )
    finally:
        await instance.aclose()
    assert result.status == "awaiting_approval", result
    assert not sync.called


@pytest.mark.asyncio
async def test_inventory_sync_returns_accepted_with_the_request_id(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUESTS).respond(200, json=[_request("req-old", "COMPLETED", 1)])
            sync = mock.post(_SYNC).respond(200, json={"requestId": "req-sync"})
            answer = await instance.product_inventory_sync(
                _operator(), _Target(), {"environment_id": _ENV_ID, "product_id": _PRODUCT_ID}
            )
    finally:
        await instance.aclose()
    assert sync.call_count == 1
    assert answer == {
        "environment_id": _ENV_ID,
        "product_id": _PRODUCT_ID,
        "status_read": "fleet.request.get",
        "status": "accepted",
        "request_id": "req-sync",
    }


@pytest.mark.asyncio
async def test_inventory_sync_does_not_start_a_second_one(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    running = _request(
        "req-running",
        "INPROGRESS",
        9,
        requestType="PRODUCT_INVENTORY_SYNC",
        inputMap={"environmentId": _ENV_ID, "productId": _PRODUCT_ID},
    )
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUESTS).respond(200, json=[running])
            sync = mock.post(_SYNC).respond(200, json={"requestId": "req-new"})
            answer = await instance.product_inventory_sync(
                _operator(), _Target(), {"environment_id": _ENV_ID, "product_id": _PRODUCT_ID}
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "already_running"
    assert answer["request_id"] == "req-running"
    assert not sync.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_map",
    [
        {
            "environmentId": "env-0002",
            "productId": "vra",
            "note": f"copied from {_ENV_ID} {_PRODUCT_ID}",
        },
        {"environmentId": _ENV_ID, "productId": f"{_PRODUCT_ID}-cloud-proxy"},
        {"environmentId": f"{_ENV_ID}-copy", "productId": _PRODUCT_ID},
        {"description": f"{_ENV_ID} {_PRODUCT_ID}"},
    ],
    ids=["other-ids-text-mentions-ours", "longer-product-id", "longer-env-id", "no-id-inputs"],
)
async def test_inventory_sync_starts_when_no_running_sync_names_exactly_these_ids(
    _stub_embedding: None, session: AsyncSession, input_map: dict[str, str]
) -> None:
    instance = await _bind()
    running = _request(
        "req-other", "INPROGRESS", 9, requestType="PRODUCT_INVENTORY_SYNC", inputMap=input_map
    )
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUESTS).respond(200, json=[running])
            sync = mock.post(_SYNC).respond(200, json={"requestId": "req-new"})
            answer = await instance.product_inventory_sync(
                _operator(), _Target(), {"environment_id": _ENV_ID, "product_id": _PRODUCT_ID}
            )
    finally:
        await instance.aclose()
    assert answer["status"] == "accepted"
    assert answer["request_id"] == "req-new"
    assert sync.call_count == 1


@pytest.mark.asyncio
async def test_inventory_sync_vendor_error_reaches_the_answer(
    _stub_embedding: None, session: AsyncSession
) -> None:
    """With approval out of the way, the vendor's error text is in the result."""
    instance = await _bind()
    await session.execute(
        update(EndpointDescriptor)
        .where(EndpointDescriptor.op_id == "fleet.product.inventory_sync")
        .values(requires_approval=False, safety_level="safe")
    )
    await session.commit()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(_REQUESTS).respond(200, json=[])
            mock.post(_SYNC).respond(
                404, json={"errorCode": "LCM-ERR-0404", "message": "Product not in environment."}
            )
            result = await _dispatch(
                "fleet.product.inventory_sync",
                {"environment_id": _ENV_ID, "product_id": _PRODUCT_ID},
            )
    finally:
        await instance.aclose()
    assert result.status == "error", result
    assert "Product not in environment." in json.dumps(result.extras)


# ---------------------------------------------------------------------------
# Environment, health, datacenters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_environment_get_replaces_secret_values(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    environment = {
        "environmentId": _ENV_ID,
        "environmentName": "<environment-name>",
        "status": "COMPLETED",
        "products": [
            {
                "id": _PRODUCT_ID,
                "version": "8.18.0",
                "properties": {"productPassword": "<clear-text>", "licenseKey": "<key>"},
                "nodes": [{"type": "master", "properties": {"vmPassword": "<clear-text>"}}],
            }
        ],
    }
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get(f"/lcm/lcops/api/v2/environments/{_ENV_ID}").respond(200, json=environment)
            result = await _dispatch("fleet.environment.get", {"environment_id": _ENV_ID})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    product = result.result["products"][0]
    assert product["version"] == "8.18.0"
    assert product["properties"]["productPassword"] == REDACTED
    assert product["nodes"][0]["properties"]["vmPassword"] == REDACTED
    assert "<clear-text>" not in json.dumps(result.result)
    assert "<key>" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_health_reports_services_and_a_failed_part(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get("/lcm/lcops/api/v2/settings/system-details").respond(
                404, json={"errorCode": "LCM-ERR-0404", "message": "Not found."}
            )
            mock.get("/lcm/health/api/v2/status").respond(
                200, json={"vrlcm-server": "UP", "postgres": "UP", "bootstrap": "DOWN"}
            )
            result = await _dispatch("fleet.health", {})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    answer = result.result
    assert answer["all_up"] is False
    assert {"service": "bootstrap", "state": "DOWN"} in answer["services"]
    assert "LCM-ERR-0404" in answer["system_details_error"]
    assert answer["health_error"] is None


@pytest.mark.asyncio
async def test_datacenter_list_flattens_and_drops_the_vcenter_password(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _bind()
    datacenters = [
        {"dataCenterVmid": "dc-1", "dataCenterName": "<dc-one>", "primaryLocation": "<place>"},
        {"dataCenterVmid": "dc-2", "dataCenterName": "<dc-two>", "primaryLocation": "<place>"},
    ]
    vcenter = {
        "vCenterName": "<vcenter-name>",
        "vCenterHost": "<vcenter-host>",
        "vcUsername": "<user>",
        "vcPassword": "<clear-text>",
        "vcUsedAs": "MANAGEMENT",
        "vcDataCollectionStatus": "COMPLETED",
    }
    try:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.get("/lcm/lcops/api/v2/datacenters").respond(200, json=datacenters)
            mock.get("/lcm/lcops/api/v2/datacenters/dc-1/vcenters").respond(200, json=[vcenter])
            mock.get("/lcm/lcops/api/v2/datacenters/dc-2/vcenters").respond(200, json=[])
            result = await _dispatch("fleet.datacenter.list", {})
    finally:
        await instance.aclose()
    assert result.status == "ok", result.error
    rows = result.result["rows"]
    assert [(row["datacenter_id"], row["vcenter_name"]) for row in rows] == [
        ("dc-1", "<vcenter-name>"),
        ("dc-2", None),
    ]
    assert "<clear-text>" not in json.dumps(result.result)
