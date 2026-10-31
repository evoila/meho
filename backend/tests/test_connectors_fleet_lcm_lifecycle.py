# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The fleet-lcm lifecycle changes and the task status read (VCF 9.1 and newer).

Drives ``fleet-lcm.task.retry`` / ``.task.cancel``, ``fleet-lcm.upgrade-plan.create``
/ ``.precheck`` / ``.apply``, ``fleet-lcm.depot.sync`` and the status read
``fleet-lcm.task.info`` through the real dispatch stack against a respx-mocked
fleet lifecycle service. No live system and no Vault: the credential loader is a
stub. The answers are written by hand from the vendor file; all names and ids
are made up.

What is proven:

* each op has the risk level of the task table, and every change needs approval;
* each change waits for approval when a person calls it, runs with the right
  request after approval, and passes a vendor error text on to the answer;
* a start answers ``accepted`` with the task id and names the status read; a
  pre-check or apply that already runs is returned and nothing new starts;
* create checks the plan first: a failed check creates nothing, a failed create
  lists what ran, what failed and what did not run;
* the status read gives state, progress and errors, says ``not_reachable`` (not
  failed) when the service does not answer, and logs in again after the login
  expired;
* no password or token reaches an answer, and no password can be passed in.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import func, select
from tenacity import wait_none

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.fleet_lcm import (
    FLEET_LCM_CONNECTOR_ID,
    FLEET_LCM_IMPL_ID,
    FLEET_LCM_PRODUCT,
    FLEET_LCM_TYPED_OPS,
    FLEET_LCM_VERSION,
    FleetLcmConnector,
    lifecycle_handlers,
    register_fleet_lcm_typed_operations,
)
from meho_backplane.connectors.fleet_lcm.lifecycle_ops import FLEET_LCM_LIFECYCLE_OPS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    reset_handler_cache,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004477")
_TARGET_NAME = "fleet-lcm-lifecycle"
_HOST = "fleet-lcm-lifecycle.test.invalid"
_BASE_URL = f"https://{_HOST}"
_TASK_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
_PLAN_ID = "22222222-aaaa-4bbb-8ccc-000000000002"
_CHECK_ID = "33333333-aaaa-4bbb-8ccc-000000000003"
_COMPONENT_ID = "44444444-aaaa-4bbb-8ccc-000000000004"
_TASK_PATH = f"/v1/tasks/{_TASK_ID}"
_PLAN_PATH = f"/v1/upgrade-plans/{_PLAN_ID}"

_PASSWORD = "<fleet-password-must-not-leak>"  # NOSONAR
_OPS_TOKEN = "<ops-token-must-not-leak>"
_JWT = "<fleet-jwt-must-not-leak>"

#: The task table: op id -> (risk level, needs approval).
_RISK_TABLE: dict[str, tuple[str, bool]] = {
    "fleet-lcm.task.retry": ("dangerous", True),
    "fleet-lcm.task.cancel": ("dangerous", True),
    "fleet-lcm.upgrade-plan.create": ("caution", True),
    "fleet-lcm.upgrade-plan.precheck": ("caution", True),
    "fleet-lcm.upgrade-plan.apply": ("dangerous", True),
    "fleet-lcm.depot.sync": ("caution", True),
    "fleet-lcm.task.info": ("safe", False),
}

_CREATE_PARAMS: dict[str, Any] = {
    "desiredSoftware": {
        "version": "<release-version>",
        "components": [{"type": "OPS", "version": "<ops-version>"}],
    },
    "componentsFilter": [{"id": _COMPONENT_ID}],
}


def _operator(sub: str = "fleet-lcm-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Fleet LCM Test Operator",
        email=None,
        raw_jwt="<fleet-lcm-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _login_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<fleet-user>", "password": _PASSWORD}


def _task(status: str = "RUNNING", **extra: Any) -> dict[str, Any]:
    return {"id": _TASK_ID, "name": "made-up-task", "type": "precheck", "status": status, **extra}


def _error_body(text: str) -> dict[str, Any]:
    return {
        "code": "BAD_REQUEST",
        "message": {"id": "made.up", "defaultMessage": text, "localizedMessage": text},
        "resolution": {"defaultMessage": "made up"},
        "referenceId": "ref-1",
        "timestamp": "2026-10-31T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=FLEET_LCM_PRODUCT,
        version=FLEET_LCM_VERSION,
        impl_id=FLEET_LCM_IMPL_ID,
        cls=FleetLcmConnector,
    )
    monkeypatch.setattr(lifecycle_handlers, "VALIDATION_POLL_SECONDS", 0.0)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
async def fleet() -> AsyncIterator[FleetLcmConnector]:
    set_default_reducer(PassThroughReducer())
    await register_fleet_lcm_typed_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product=FLEET_LCM_PRODUCT,
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="fleet/lifecycle",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.1.0"},
                notes="seeded by test_connectors_fleet_lcm_lifecycle",
            )
        )
        await session.commit()
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    _CONNECTOR_INSTANCE_CACHE[FleetLcmConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an op as an already-approved call."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=FLEET_LCM_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _mount_login(mock: respx.MockRouter) -> respx.Route:
    acquire = mock.post("/suite-api/api/auth/token/acquire").respond(
        200, json={"token": _OPS_TOKEN}
    )
    mock.post("/suite-api/api/auth/token/exchange").respond(
        200, json={"jwtToken": _JWT, "services": []}
    )
    return acquire


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


def _assert_no_secret(answer: dict[str, Any]) -> None:
    text = json.dumps(answer)
    for secret in (_PASSWORD, _OPS_TOKEN, _JWT):
        assert secret not in text


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------


def test_risk_table_matches_the_task() -> None:
    """Each op has the risk level of the task table; every change needs approval."""
    table = {
        op.op_id: (op.safety_level, op.requires_approval)
        for op in FLEET_LCM_TYPED_OPS
        if op.op_id in _RISK_TABLE
    }
    assert table == _RISK_TABLE
    assert {op.op_id for op in FLEET_LCM_LIFECYCLE_OPS} == set(_RISK_TABLE) - {
        "fleet-lcm.task.info"
    }


async def test_registered_rows_carry_the_risk_levels(fleet: FleetLcmConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_(list(_RISK_TABLE))
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id: (row.safety_level, row.requires_approval) for row in rows} == _RISK_TABLE
    assert all(row.source_kind == "typed" and row.is_enabled for row in rows)


# ---------------------------------------------------------------------------
# Every change waits for approval
# ---------------------------------------------------------------------------

_CHANGE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("fleet-lcm.task.retry", {"taskId": _TASK_ID}),
    ("fleet-lcm.task.cancel", {"taskId": _TASK_ID}),
    ("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS),
    ("fleet-lcm.upgrade-plan.precheck", {"planId": _PLAN_ID}),
    ("fleet-lcm.upgrade-plan.apply", {"planId": _PLAN_ID}),
    ("fleet-lcm.depot.sync", {}),
]


@pytest.mark.parametrize(("op_id", "params"), _CHANGE_CALLS, ids=[c[0] for c in _CHANGE_CALLS])
async def test_each_change_waits_for_approval(
    fleet: FleetLcmConnector, op_id: str, params: dict[str, Any]
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        vendor = mock.route(url__startswith=f"{_BASE_URL}/v1/").respond(200, json=_task())
        call = await call_operation(
            _operator(),
            {
                "connector_id": FLEET_LCM_CONNECTOR_ID,
                "op_id": op_id,
                "target": _TARGET_NAME,
                "params": params,
            },
        )
    assert call["status"] == "awaiting_approval", call
    assert not vendor.called
    assert await _approval_rows() == 1


# ---------------------------------------------------------------------------
# Task retry and cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["retry", "cancel"])
async def test_task_action_answers_accepted_with_the_status_read(
    fleet: FleetLcmConnector, action: str
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        post = mock.post(_TASK_PATH, params={"action": action}).respond(200, json=_task())
        result = await _run(f"fleet-lcm.task.{action}", {"taskId": _TASK_ID})
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["status"] == "accepted"
    assert answer["started"] is True
    assert answer["task_id"] == _TASK_ID
    assert answer["status_read"] == {
        "op_id": "fleet-lcm.task.info",
        "params": {"taskId": _TASK_ID},
    }
    assert "not done" in answer["note"]
    request = post.calls.last.request
    assert str(request.url) == f"{_BASE_URL}{_TASK_PATH}?action={action}"
    assert request.headers["Authorization"] == f"Bearer {_JWT}"
    _assert_no_secret(result)


@pytest.mark.parametrize("action", ["retry", "cancel"])
async def test_task_action_vendor_error_reaches_the_answer(
    fleet: FleetLcmConnector, action: str
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post(_TASK_PATH, params={"action": action}).respond(
            400, json=_error_body("Task is not in a state that allows this action.")
        )
        result = await _run(f"fleet-lcm.task.{action}", {"taskId": _TASK_ID})
    assert result["status"] == "error", result
    assert "Task is not in a state that allows this action." in json.dumps(result)


# ---------------------------------------------------------------------------
# Upgrade plan pre-check and apply
# ---------------------------------------------------------------------------

_PLAN_ACTIONS = [("precheck", "PRECHECK"), ("apply", "UPGRADE")]


@pytest.mark.parametrize(("action", "run_type"), _PLAN_ACTIONS)
async def test_plan_action_starts_the_job(
    fleet: FleetLcmConnector, action: str, run_type: str
) -> None:
    other_type = "UPGRADE" if run_type == "PRECHECK" else "PRECHECK"
    plan = {
        "id": _PLAN_ID,
        "executions": [
            {"taskId": "old-run", "type": run_type, "status": "SUCCEEDED"},
            {"taskId": "other-kind", "type": other_type, "status": "RUNNING"},
        ],
    }
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_PLAN_PATH).respond(200, json=plan)
        post = mock.post(_PLAN_PATH, params={"action": action}).respond(202, json=_task())
        result = await _run(
            f"fleet-lcm.upgrade-plan.{action}",
            {"planId": _PLAN_ID, "componentsFilter": [{"id": _COMPONENT_ID}]},
        )
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["status"] == "accepted"
    assert answer["task_id"] == _TASK_ID
    assert answer["status_read"]["op_id"] == "fleet-lcm.task.info"
    assert json.loads(post.calls.last.request.content) == {
        "componentsFilter": [{"id": _COMPONENT_ID}]
    }


@pytest.mark.parametrize(("action", "run_type"), _PLAN_ACTIONS)
async def test_plan_action_returns_the_running_job_and_starts_nothing(
    fleet: FleetLcmConnector, action: str, run_type: str
) -> None:
    plan = {
        "id": _PLAN_ID,
        "executions": [{"taskId": "running-run", "type": run_type, "status": "RUNNING"}],
    }
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_PLAN_PATH).respond(200, json=plan)
        post = mock.post(_PLAN_PATH, params={"action": action}).respond(202, json=_task())
        result = await _run(f"fleet-lcm.upgrade-plan.{action}", {"planId": _PLAN_ID})
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["status"] == "already_running"
    assert answer["started"] is False
    assert answer["task_id"] == "running-run"
    assert answer["status_read"]["params"] == {"taskId": "running-run"}
    assert not post.called


@pytest.mark.parametrize(("action", "run_type"), _PLAN_ACTIONS)
async def test_plan_action_vendor_error_reaches_the_answer(
    fleet: FleetLcmConnector, action: str, run_type: str
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_PLAN_PATH).respond(200, json={"id": _PLAN_ID, "executions": []})
        mock.post(_PLAN_PATH, params={"action": action}).respond(
            400, json=_error_body("The plan is not ready.")
        )
        result = await _run(f"fleet-lcm.upgrade-plan.{action}", {"planId": _PLAN_ID})
    assert result["status"] == "error", result
    assert "The plan is not ready." in json.dumps(result)


# ---------------------------------------------------------------------------
# Depot sync
# ---------------------------------------------------------------------------


async def test_depot_sync_answers_accepted(fleet: FleetLcmConnector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        post = mock.post("/v1/depot-metadata", params={"action": "sync"}).respond(
            202, json=_task(type="DEPOT_SYNC")
        )
        result = await _run("fleet-lcm.depot.sync", {})
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "accepted"
    assert result["result"]["task_id"] == _TASK_ID
    assert post.called


async def test_depot_sync_vendor_error_reaches_the_answer(fleet: FleetLcmConnector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/v1/depot-metadata", params={"action": "sync"}).respond(
            500, json=_error_body("The depot cannot be reached.")
        )
        result = await _run("fleet-lcm.depot.sync", {})
    assert result["status"] == "error", result
    assert "The depot cannot be reached." in json.dumps(result)


# ---------------------------------------------------------------------------
# Upgrade plan create: check first, then create
# ---------------------------------------------------------------------------


def _check_task(status: str, **extra: Any) -> dict[str, Any]:
    return {"id": _CHECK_ID, "type": "validate", "status": status, **extra}


async def test_create_checks_the_plan_then_creates_it(fleet: FleetLcmConnector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        check = mock.post("/v1/upgrade-plans/validations").respond(202, json=_check_task("RUNNING"))
        poll = mock.get(f"/v1/tasks/{_CHECK_ID}").respond(200, json=_check_task("SUCCEEDED"))
        create = mock.post("/v1/upgrade-plans").respond(
            201, json={"id": _PLAN_ID, "status": "DRAFT"}
        )
        result = await _run("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS)
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["outcome"] == "created"
    assert answer["plan_id"] == _PLAN_ID
    assert answer["steps_ran"] == ["validate", "create"]
    assert answer["step_failed"] is None
    assert answer["steps_not_run"] == []
    assert poll.called
    spec = json.loads(check.calls.last.request.content)
    assert spec == _CREATE_PARAMS
    assert json.loads(create.calls.last.request.content) == {"spec": _CREATE_PARAMS}


async def test_create_creates_nothing_when_the_check_fails(fleet: FleetLcmConnector) -> None:
    failed_check = _check_task(
        "SUCCEEDED",
        result={
            "type": "VALIDATION",
            "failedValidations": [
                {
                    "severity": "ERROR",
                    "errorCode": "COMPONENT_NOT_FOUND",
                    "message": {"defaultMessage": "Component not found."},
                },
                {"severity": "WARNING", "message": {"defaultMessage": "Made-up warning."}},
            ],
        },
    )
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/v1/upgrade-plans/validations").respond(202, json=failed_check)
        create = mock.post("/v1/upgrade-plans").respond(201, json={"id": _PLAN_ID})
        result = await _run("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS)
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["outcome"] == "validation_failed"
    assert answer["steps_ran"] == []
    assert answer["step_failed"]["name"] == "validate"
    assert answer["step_failed"]["errors"] == ["Component not found."]
    assert answer["step_failed"]["warnings"] == ["Made-up warning."]
    assert answer["steps_not_run"] == ["create"]
    assert not create.called


async def test_create_reports_a_failed_check_job(fleet: FleetLcmConnector) -> None:
    failed_check = _check_task(
        "FAILED",
        stages=[
            {
                "name": "check",
                "status": "FAILED",
                "messages": [{"level": "ERROR", "message": {"defaultMessage": "Bad version."}}],
            }
        ],
    )
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/v1/upgrade-plans/validations").respond(202, json=failed_check)
        create = mock.post("/v1/upgrade-plans").respond(201, json={"id": _PLAN_ID})
        result = await _run("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS)
    assert result["result"]["outcome"] == "validation_failed"
    assert result["result"]["step_failed"]["errors"] == ["Bad version."]
    assert not create.called


async def test_create_stops_when_the_check_still_runs(
    fleet: FleetLcmConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle_handlers, "VALIDATION_WAIT_SECONDS", 0.0)
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/v1/upgrade-plans/validations").respond(202, json=_check_task("RUNNING"))
        create = mock.post("/v1/upgrade-plans").respond(201, json={"id": _PLAN_ID})
        result = await _run("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS)
    answer = result["result"]
    assert answer["outcome"] == "validation_still_running"
    assert answer["step_failed"]["task_id"] == _CHECK_ID
    assert answer["steps_not_run"] == ["create"]
    assert not create.called


async def test_create_failing_at_step_two_lists_what_ran(fleet: FleetLcmConnector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/v1/upgrade-plans/validations").respond(202, json=_check_task("SUCCEEDED"))
        mock.post("/v1/upgrade-plans").respond(
            400, json=_error_body("The release is not in the depot.")
        )
        result = await _run("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS)
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["outcome"] == "create_failed"
    assert answer["steps_ran"] == ["validate"]
    assert answer["step_failed"]["name"] == "create"
    assert answer["step_failed"]["errors"] == ["HTTP 400: The release is not in the depot."]
    assert answer["steps_not_run"] == []
    assert answer["plan"] is None


async def test_create_vendor_error_at_the_check_reaches_the_answer(
    fleet: FleetLcmConnector,
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post("/v1/upgrade-plans/validations").respond(
            400, json=_error_body("desiredSoftware.version is missing.")
        )
        result = await _run("fleet-lcm.upgrade-plan.create", _CREATE_PARAMS)
    assert result["status"] == "error", result
    assert "desiredSoftware.version is missing." in json.dumps(result)


async def test_no_password_can_be_passed_in(fleet: FleetLcmConnector) -> None:
    params = {
        "desiredSoftware": {"version": "<release-version>"},
        "componentsFilter": [
            {"id": _COMPONENT_ID, "userInputSpec": {"targetVspClusterPassword": _PASSWORD}}
        ],
    }
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        vendor = mock.route(url__startswith=f"{_BASE_URL}/v1/").respond(200, json={})
        result = await _run("fleet-lcm.upgrade-plan.create", params)
    assert result["status"] == "error", result
    assert result["extras"]["error_code"] == "invalid_params"
    assert not vendor.called


# ---------------------------------------------------------------------------
# The status read: fleet-lcm.task.info
# ---------------------------------------------------------------------------


async def test_status_read_gives_state_progress_and_errors(fleet: FleetLcmConnector) -> None:
    task = _task(
        "FAILED",
        retriable=True,
        stages=[
            {"name": "download", "status": "SUCCEEDED"},
            {
                "name": "upgrade",
                "status": "FAILED",
                "messages": [
                    {"level": "INFO", "message": {"defaultMessage": "Started."}},
                    {"level": "ERROR", "message": {"localizedMessage": "Disk is full."}},
                ],
            },
        ],
        subTasks=[
            {
                "id": "sub-1",
                "messages": [{"level": "ERROR", "message": {"defaultMessage": "Node down."}}],
            }
        ],
    )
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_TASK_PATH).respond(200, json=task)
        result = await _run("fleet-lcm.task.info", {"taskId": _TASK_ID})
    assert result["status"] == "ok", result
    answer = result["result"]
    assert answer["task_id"] == _TASK_ID
    assert answer["state"] == "failed"
    assert answer["status"] == "FAILED"
    assert answer["errors"] == ["Disk is full.", "Node down."]
    assert answer["progress"] == {"stages_total": 2, "stages_done": 1, "current_stage": None}
    assert answer["reachable"] is True
    assert answer["checked_at"]
    _assert_no_secret(result)


@pytest.mark.parametrize(
    "status",
    [("PENDING", "queued"), ("RUNNING", "running"), ("SUCCEEDED", "succeeded")],
    ids=lambda s: s[0],
)
async def test_status_read_maps_the_vendor_state(
    fleet: FleetLcmConnector, status: tuple[str, str]
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_TASK_PATH).respond(200, json=_task(status[0]))
        result = await _run("fleet-lcm.task.info", {"taskId": _TASK_ID})
    assert result["result"]["state"] == status[1]


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("refused"), httpx.Response(503)],
    ids=["no-connection", "http-503"],
)
async def test_status_read_says_not_reachable_not_failed(
    fleet: FleetLcmConnector, answer: Any
) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        route = mock.get(_TASK_PATH)
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        with patch.object(fleet._request_json.retry, "wait", wait_none()):  # type: ignore[attr-defined]
            result = await _run("fleet-lcm.task.info", {"taskId": _TASK_ID})
    assert result["status"] == "ok", result
    body = result["result"]
    assert body["state"] == "not_reachable"
    assert body["reachable"] is False
    assert body["task_id"] == _TASK_ID
    assert body["checked_at"]
    assert body["unreachable_reason"]


async def test_status_read_logs_in_again_after_the_login_expired(
    fleet: FleetLcmConnector,
) -> None:
    """Start a job, the login expires, the status read logs in again and reads the job."""
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        acquire = _mount_login(mock)
        mock.post("/v1/depot-metadata", params={"action": "sync"}).respond(202, json=_task())
        started = await _run("fleet-lcm.depot.sync", {})
        assert started["result"]["status"] == "accepted"
        read = mock.get(_TASK_PATH)
        read.side_effect = [httpx.Response(401), httpx.Response(200, json=_task("SUCCEEDED"))]
        result = await _run("fleet-lcm.task.info", {"taskId": started["result"]["task_id"]})
    assert result["status"] == "ok", result
    assert result["result"]["state"] == "succeeded"
    assert acquire.call_count == 2
    assert read.call_count == 2
    _assert_no_secret(result)
