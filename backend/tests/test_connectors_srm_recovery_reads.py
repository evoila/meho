# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the SRM pairing, remote login, plan, group, issue and history reads.

Every read runs through ``dispatch()`` against a respx-mocked SRM REST API
v2, with saved answers written by hand from the vendor API reference:

* ``srm.pairing.list``: one flat row per pairing, paging, no certificate text;
* ``srm.remote_login``: the remote account from Vault in HTTP Basic next to
  the SRM session; a second ``401`` is a refused remote login; a secret
  without a password sends nothing; no password in the answer;
* ``srm.plan.list``: state and ``needs_cleanup``;
* ``srm.plan.get``: plan, groups, steps, VMs and one VM's settings; a view
  mode SRM refuses comes back as ``steps_error``;
* ``srm.group.list``: groups, and the VMs of one group;
* ``srm.issue.list``: each scope reaches its path; two scopes are refused
  before any HTTP request;
* ``srm.history.list``: the task is checked until it ends; a failed task
  and one report;
* a vendor error reaches the answer; an id that is not one path segment
  is refused.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.srm import (
    SRM_CONNECTOR_ID,
    SRM_IMPL_ID,
    SRM_PRODUCT,
    SRM_VERSION,
    SrmConnector,
    recovery_reads,
    register_srm_typed_operations,
)
from meho_backplane.connectors.srm.recovery_ops import RECOVERY_TYPED_OPS
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_HOST = "srm-recovery.test.invalid"
_BASE_URL = f"https://{_HOST}"
_API = "/api/rest/srm/v2"
_PAIRING = "pairing-0001"
_P = f"{_API}/pairings/{_PAIRING}"
_PLAN = f"{_P}/recovery-management/plans/plan-0001"
_PASSWORD = "local-password-value"
_REMOTE_PASSWORD = "remote-password-value"
_REMOTE_REF = "targets/recovery-reads/remote-site"
_PEM = "-----BEGIN CERTIFICATE-----\nMIIBmadeup\n-----END CERTIFICATE-----"


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
        product=SRM_PRODUCT, version=SRM_VERSION, impl_id=SRM_IMPL_ID, cls=SrmConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _fast_history_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery_reads, "HISTORY_POLL_SECONDS", 0.0)


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    from meho_backplane.db.engine import get_sessionmaker

    async with get_sessionmaker()() as s:
        yield s


class _Target:
    """Target satisfying the connector Protocols and the resolver shape."""

    def __init__(self) -> None:
        self.product = SRM_PRODUCT
        self.fingerprint = type("_FP", (), {"version": SRM_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id = UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = f"srm-recovery-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.fqdn = None
        self.secret_ref = "targets/recovery-reads/srm"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


def _operator() -> Operator:
    return Operator(
        sub="op-recovery-srm",
        name="SRM Recovery Operator",
        email=None,
        raw_jwt="op.recovery.srm.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "srm-local-svc", "password": _PASSWORD}


@pytest.fixture
async def connector(_stub_embedding: AsyncMock, session: AsyncSession) -> SrmConnector:
    await register_srm_typed_operations()
    instance = get_or_create_connector_instance(SrmConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, SrmConnector)
    return instance


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"{_API}/session").respond(200, json={"session_id": "srm-session-1"})
        yield mock


async def _dispatch(op_id: str, params: dict[str, Any], target: _Target | None = None) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=SRM_CONNECTOR_ID,
        op_id=op_id,
        target=target or _Target(),
        params=params,
    )


def _page(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"list": rows, "_meta": {"total": len(rows) if total is None else total}}


def _server(name: str, status: str = "OK") -> dict[str, Any]:
    return {
        "id": f"{name}-id",
        "url": f"https://{name}:443",
        "name": name,
        "server_status": status,
        "version": "9.1.0",
        "build_number": "22222222",
        "certificates": [{"certificate": _PEM, "thumbprint": "AA:BB"}],
    }


# ---------------------------------------------------------------------------
# Pairings and the remote login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_list_returns_one_flat_row_per_pairing(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    pairing = {
        "pairing_id": _PAIRING,
        "local_srm_server": _server("<srm-a>"),
        "remote_srm_server": _server("<srm-b>", "WARNING"),
        "local_vc_server": _server("<vc-a>"),
        "remote_vc_server": _server("<vc-b>"),
    }
    route = api.get(f"{_API}/pairings").respond(200, json=_page([pairing]))
    result = await _dispatch("srm.pairing.list", {})

    assert result.status == "ok", result.error
    assert route.calls[0].request.headers["x-dr-session"] == "srm-session-1"
    assert result.result["total"] == 1
    row = result.result["items"][0]
    assert row["pairing_id"] == _PAIRING
    assert row["local_srm_name"] == "<srm-a>"
    assert row["remote_srm_status"] == "WARNING"
    assert row["remote_vc_id"] == "<vc-b>-id"
    assert all(not isinstance(value, dict | list) for value in row.values())
    assert _PEM not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_list_reads_every_page(
    connector: SrmConnector, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery_reads, "PAGE_SIZE", 1)
    route = api.get(f"{_P}/recovery-management/plans").mock(
        side_effect=[
            httpx.Response(200, json=_page([{"id": "plan-a", "status": "READY_STATE"}], 2)),
            httpx.Response(200, json=_page([{"id": "plan-b", "status": "READY_STATE"}], 2)),
        ]
    )
    result = await _dispatch("srm.plan.list", {"pairing_id": _PAIRING})

    assert result.status == "ok", result.error
    assert [row["id"] for row in result.result["items"]] == ["plan-a", "plan-b"]
    assert [call.request.url.params["offset"] for call in route.calls] == ["0", "1"]


@pytest.fixture
def _remote_secret(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    loader = AsyncMock(return_value={"username": "remote-svc", "password": _REMOTE_PASSWORD})
    monkeypatch.setattr(recovery_reads, "load_param_secret_data", loader)
    return loader


@pytest.mark.asyncio
async def test_remote_login_sends_the_vault_account_and_never_returns_it(
    connector: SrmConnector, api: respx.MockRouter, _remote_secret: AsyncMock
) -> None:
    post = api.post(f"{_P}/remote-session").respond(204)
    api.get(f"{_P}/remote-session").respond(200, json={"username": "remote-svc"})
    result = await _dispatch(
        "srm.remote_login", {"pairing_id": _PAIRING, "secret_ref": _REMOTE_REF}
    )

    assert result.status == "ok", result.error
    assert result.result == {
        "pairing_id": _PAIRING,
        "logged_in": True,
        "remote_username": "remote-svc",
    }
    request = post.calls[0].request
    assert request.headers["x-dr-session"] == "srm-session-1"
    expected = base64.b64encode(f"remote-svc:{_REMOTE_PASSWORD}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert _remote_secret.await_args.kwargs["param"] == "secret_ref"
    assert _remote_secret.await_args.args[0].secret_ref == _REMOTE_REF
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert _REMOTE_PASSWORD not in rendered
    assert _PASSWORD not in rendered


@pytest.mark.asyncio
async def test_remote_login_refused_twice_names_the_remote_account(
    connector: SrmConnector, api: respx.MockRouter, _remote_secret: AsyncMock
) -> None:
    post = api.post(f"{_P}/remote-session").respond(401)
    result = await _dispatch(
        "srm.remote_login", {"pairing_id": _PAIRING, "secret_ref": _REMOTE_REF}
    )

    assert result.status == "error"
    assert post.call_count == 2
    assert api.routes[0].call_count == 2  # the SRM login ran again before the retry
    rendered = (result.error or "") + json.dumps(result.extras, default=str)
    assert "refused the login" in rendered
    assert _REMOTE_PASSWORD not in rendered


@pytest.mark.asyncio
async def test_remote_login_without_a_password_sends_nothing(
    connector: SrmConnector, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recovery_reads, "load_param_secret_data", AsyncMock(return_value={"username": "x"})
    )
    post = api.post(f"{_P}/remote-session").respond(204)
    result = await _dispatch(
        "srm.remote_login", {"pairing_id": _PAIRING, "secret_ref": _REMOTE_REF}
    )

    assert result.status == "error"
    assert post.call_count == 0
    assert "Nothing was sent" in (result.error or "") + json.dumps(result.extras, default=str)


@pytest.mark.parametrize(
    "secret_ref",
    [
        "targets/other-site/login",
        "other/recovery-reads/remote-site",
        "targets/recovery-reads/../remote-site",
        "gsm:proj/recovery-reads/remote-site",
    ],
)
@pytest.mark.asyncio
async def test_remote_login_refuses_a_secret_ref_outside_the_allowed_prefix(
    connector: SrmConnector, api: respx.MockRouter, _remote_secret: AsyncMock, secret_ref: str
) -> None:
    route = api.route(url__regex=r".*").respond(204)
    result = await _dispatch("srm.remote_login", {"pairing_id": _PAIRING, "secret_ref": secret_ref})

    assert result.status == "error"
    rendered = (result.error or "") + json.dumps(result.extras, default=str)
    assert "outside the allowed secret prefix" in rendered
    assert _remote_secret.await_count == 0
    assert route.call_count == 0
    assert api.routes[0].call_count == 0  # not even the SRM login


@pytest.mark.asyncio
async def test_remote_login_uses_the_prefix_an_operator_set_in_extras(
    connector: SrmConnector, api: respx.MockRouter, _remote_secret: AsyncMock
) -> None:
    post = api.post(f"{_P}/remote-session").respond(204)
    api.get(f"{_P}/remote-session").respond(200, json={"username": "remote-svc"})
    target = _Target()
    target.extras = {"remote_secret_prefix": "remote-sites/site-b"}

    allowed = await _dispatch(
        "srm.remote_login",
        {"pairing_id": _PAIRING, "secret_ref": "remote-sites/site-b/login"},
        target,
    )
    assert allowed.status == "ok", allowed.error
    assert post.call_count == 1
    assert _remote_secret.await_args.args[0].secret_ref == "remote-sites/site-b/login"

    refused = await _dispatch(
        "srm.remote_login",
        {"pairing_id": _PAIRING, "secret_ref": _REMOTE_REF},
        target,
    )
    assert refused.status == "error"
    assert _remote_secret.await_count == 1
    assert post.call_count == 1


# ---------------------------------------------------------------------------
# Recovery plans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_list_marks_plans_that_need_a_cleanup(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    plans = [
        {"id": "plan-a", "name": "<plan-a>", "status": "READY_STATE", "is_running": False},
        {"id": "plan-b", "name": "<plan-b>", "status": "TEST_COMPLETE", "is_running": False},
        {"id": "plan-c", "name": "<plan-c>", "status": "RECOVERY_IN_PROGRESS", "progress": 40},
    ]
    api.get(f"{_P}/recovery-management/plans").respond(200, json=_page(plans))
    result = await _dispatch("srm.plan.list", {"pairing_id": _PAIRING})

    assert result.status == "ok", result.error
    rows = {row["id"]: row for row in result.result["items"]}
    assert rows["plan-a"]["needs_cleanup"] is False
    assert rows["plan-b"]["needs_cleanup"] is True
    assert rows["plan-c"]["progress"] == 40
    assert result.result["pairing_id"] == _PAIRING


@pytest.mark.asyncio
async def test_plan_get_reads_groups_steps_vms_and_one_vm(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    api.get(_PLAN).respond(200, json={"id": "plan-0001", "status": "READY_STATE", "_meta": {}})
    api.get(f"{_PLAN}/protection-groups").respond(
        200, json=_page([{"id": "group-1", "name": "<group-a>", "status": "OK"}])
    )
    steps = api.get(f"{_PLAN}/recovery-steps/test").respond(
        200,
        json=_page(
            [
                {
                    "id": 1,
                    "step_number": "1",
                    "title": "Synchronize storage",
                    "status": "SUCCESS",
                    "start_time": 1700000000000,
                    "finish_time": 1700000060000,
                }
            ]
        ),
    )
    api.get(f"{_PLAN}/vms").respond(
        200,
        json=_page(
            [
                {
                    "id": "vm-1",
                    "name": "<vm-name>",
                    "status": "READY_TO_RECOVER",
                    "protection_group_name": "<group-a>",
                    "recovery_settings": {
                        "priority": "P2",
                        "startup_action": "POWER_ON",
                        "dependent_vms": [{"id": "vm-2", "name": "<other-vm>"}],
                    },
                }
            ]
        ),
    )
    api.get(f"{_PLAN}/vms/vm-1/recovery-settings").respond(
        200, json={"recovery_settings": {"priority": "P2"}, "status": "OK", "_meta": {}}
    )
    result = await _dispatch(
        "srm.plan.get",
        {"pairing_id": _PAIRING, "plan_id": "plan-0001", "view_mode": "test", "vm_id": "vm-1"},
    )

    assert result.status == "ok", result.error
    body = result.result
    assert steps.call_count == 1
    assert body["plan"]["needs_cleanup"] is False
    assert body["protection_groups"] == [{"id": "group-1", "name": "<group-a>", "status": "OK"}]
    assert body["steps"][0]["start_time"] == "2023-11-14T22:13:20Z"
    assert body["steps_error"] is None
    assert body["vms"][0]["priority"] == "P2"
    assert body["vms"][0]["dependent_vms"] == ["<other-vm>"]
    assert body["vm_recovery_settings"] == {
        "vm_id": "vm-1",
        "recovery_settings": {"priority": "P2"},
        "status": "OK",
    }


@pytest.mark.asyncio
async def test_plan_get_reports_a_refused_view_mode_and_keeps_the_rest(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    api.get(_PLAN).respond(200, json={"id": "plan-0001", "status": "READY_STATE"})
    api.get(f"{_PLAN}/protection-groups").respond(200, json=_page([]))
    api.get(f"{_PLAN}/recovery-steps/recovery").respond(
        400, json={"message": "made-up: steps are not available in this state"}
    )
    api.get(f"{_PLAN}/vms").respond(200, json=_page([]))
    result = await _dispatch("srm.plan.get", {"pairing_id": _PAIRING, "plan_id": "plan-0001"})

    assert result.status == "ok", result.error
    assert result.result["view_mode"] == "recovery"
    assert result.result["steps"] == []
    assert "HTTP 400" in result.result["steps_error"]
    assert "not available in this state" in result.result["steps_error"]
    assert result.result["vm_recovery_settings"] is None


@pytest.mark.asyncio
async def test_vendor_error_reaches_the_answer(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    api.get(f"{_P}/recovery-management/plans").respond(
        404, json={"message": "made-up: pairing not found"}
    )
    result = await _dispatch("srm.plan.list", {"pairing_id": _PAIRING})

    assert result.status == "error"
    rendered = (result.error or "") + json.dumps(result.extras, default=str)
    assert "404" in rendered


@pytest.mark.parametrize("bad_id", ["../tasks", "..", "a?b", "a%2Fb"])
@pytest.mark.asyncio
async def test_an_id_that_is_not_one_path_segment_is_refused_before_any_request(
    connector: SrmConnector, api: respx.MockRouter, bad_id: str
) -> None:
    route = api.get(url__regex=r".*").respond(200, json=_page([]))
    result = await _dispatch("srm.plan.list", {"pairing_id": bad_id})

    assert result.status == "error"
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Protection groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_list_returns_groups_or_the_vms_of_one_group(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    groups_path = f"{_P}/protection-management/groups"
    api.get(groups_path).respond(
        200,
        json=_page(
            [
                {
                    "id": "group-1",
                    "name": "<group-a>",
                    "replication_type": "HBR",
                    "status": "OK",
                    "recovery_status": "READY",
                    "total_vms": 2,
                }
            ]
        ),
    )
    api.get(f"{groups_path}/group-1/vms").respond(
        200,
        json=_page(
            [
                {
                    "id": "vm-1",
                    "name": "<vm-name>",
                    "protection_info": {
                        "recovery_folder": {"id": "f-1", "name": "<folder>"},
                        "recovery_networks": [{"id": "n-1", "name": "<network>"}],
                    },
                    "protection_status_details": {
                        "status": "MISSING_MAPPING",
                        "errors": ["made-up: no network mapping"],
                    },
                }
            ]
        ),
    )
    groups = await _dispatch("srm.group.list", {"pairing_id": _PAIRING})
    vms = await _dispatch("srm.group.list", {"pairing_id": _PAIRING, "group_id": "group-1"})

    assert groups.status == "ok", groups.error
    assert groups.result["kind"] == "groups"
    assert groups.result["items"][0]["replication_type"] == "HBR"
    assert groups.result["items"][0]["total_vms"] == 2
    assert vms.status == "ok", vms.error
    assert vms.result["kind"] == "vms"
    assert vms.result["group_id"] == "group-1"
    row = vms.result["items"][0]
    assert row["protection_status"] == "MISSING_MAPPING"
    assert row["protection_errors"] == ["made-up: no network mapping"]
    assert row["recovery_folder"] == "<folder>"
    assert row["recovery_networks"] == ["<network>"]


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

_ISSUE = {
    "entity": "plan-0001",
    "entity_name": "<plan-a>",
    "description": "made-up issue text",
    "timestamp": 1700000000000,
    "status": "ERROR",
    "issue_type": "RECOVERY_PLAN",
}


@pytest.mark.parametrize(
    ("scope_param", "path", "scope"),
    [
        ({}, f"{_P}/issues", "pairing"),
        ({"plan_id": "plan-0001"}, f"{_PLAN}/issues", "plan"),
        ({"group_id": "group-1"}, f"{_P}/protection-management/groups/group-1/issues", "group"),
        ({"srm_id": "srm-1"}, f"{_P}/srms/srm-1/issues", "srm"),
    ],
)
@pytest.mark.asyncio
async def test_issue_list_reads_the_scope_it_is_given(
    connector: SrmConnector,
    api: respx.MockRouter,
    scope_param: dict[str, str],
    path: str,
    scope: str,
) -> None:
    route = api.get(path).respond(200, json=_page([_ISSUE, {**_ISSUE, "status": "WARNING"}]))
    result = await _dispatch("srm.issue.list", {"pairing_id": _PAIRING, **scope_param})

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert result.result["scope"] == scope
    assert result.result["errors"] == 1
    assert result.result["warnings"] == 1
    assert result.result["items"][0] == {
        "severity": "ERROR",
        "issue_type": "RECOVERY_PLAN",
        "entity": "plan-0001",
        "entity_name": "<plan-a>",
        "description": "made-up issue text",
        "time": "2023-11-14T22:13:20Z",
    }


@pytest.mark.asyncio
async def test_issue_list_refuses_two_scopes_before_any_request(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    route = api.get(url__regex=r".*/issues").respond(200, json=_page([]))
    result = await _dispatch(
        "srm.issue.list", {"pairing_id": _PAIRING, "plan_id": "plan-0001", "srm_id": "srm-1"}
    )

    assert result.status == "error"
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

_REPORT = {
    "plan_name": "<plan-a>",
    "username": "<user>",
    "operation_type": "TEST",
    "start_time_ms": 1700000000000,
    "human_friendly_duration": "5 m 10 s",
    "duration_ms": 310000,
    "result_state": "WARNINGS",
    "error_count": 0,
    "warning_count": 1,
    "run_key": 42,
    "errors_list": [],
    "warnings_list": ["made-up warning"],
}


@pytest.mark.asyncio
async def test_history_list_waits_for_the_task_and_returns_the_runs(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    reports = api.get(f"{_PLAN}/history-reports").respond(
        202, json={"id": "task-1", "status": "RUNNING"}
    )
    task = api.get(f"{_API}/tasks/task-1").mock(
        side_effect=[
            httpx.Response(200, json={"id": "task-1", "status": "RUNNING"}),
            httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "status": "SUCCESS",
                    "result": {
                        "list": [
                            {**_REPORT, "run_key": 41, "start_time_ms": 1690000000000},
                            _REPORT,
                        ]
                    },
                },
            ),
        ]
    )
    result = await _dispatch(
        "srm.history.list",
        {
            "pairing_id": _PAIRING,
            "plan_id": "plan-0001",
            "start_time": "2023-11-01T00:00:00Z",
        },
    )

    assert result.status == "ok", result.error
    assert reports.calls[0].request.url.params["start_date"] == "1698796800000"
    assert task.call_count == 2
    items = result.result["items"]
    assert [row["report_id"] for row in items] == ["42", "41"]
    assert items[0]["result_state"] == "WARNINGS"
    assert items[0]["start_time"] == "2023-11-14T22:13:20Z"
    assert "warnings" not in items[0]


@pytest.mark.asyncio
async def test_history_list_reports_a_failed_task(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    api.get(f"{_PLAN}/history-reports").respond(
        202,
        json={"id": "task-2", "status": "ERROR", "error": {"message": "made-up task error"}},
    )
    result = await _dispatch("srm.history.list", {"pairing_id": _PAIRING, "plan_id": "plan-0001"})

    assert result.status == "error"
    assert "made-up task error" in (result.error or "") + json.dumps(result.extras, default=str)


@pytest.mark.asyncio
async def test_history_list_with_report_id_returns_its_texts(
    connector: SrmConnector, api: respx.MockRouter
) -> None:
    api.get(f"{_PLAN}/history-reports/42").respond(200, json={**_REPORT, "_meta": {}})
    result = await _dispatch(
        "srm.history.list", {"pairing_id": _PAIRING, "plan_id": "plan-0001", "report_id": "42"}
    )

    assert result.status == "ok", result.error
    assert result.result["items"] == [
        {
            "report_id": "42",
            "plan_name": "<plan-a>",
            "operation_type": "TEST",
            "result_state": "WARNINGS",
            "username": "<user>",
            "start_time": "2023-11-14T22:13:20Z",
            "duration_ms": 310000,
            "duration": "5 m 10 s",
            "error_count": 0,
            "warning_count": 1,
            "errors": [],
            "warnings": ["made-up warning"],
        }
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_reads_are_registered_safe_with_group_texts(
    connector: SrmConnector, session: AsyncSession
) -> None:
    op_ids = {op.op_id for op in RECOVERY_TYPED_OPS}
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(op_ids))
            )
        )
        .scalars()
        .all()
    )
    assert {row.op_id for row in rows} == op_ids
    for row in rows:
        assert row.source_kind == "typed"
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert len(row.parameter_schema["examples"]) == 1
    groups = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.group_key.in_(
                        ["srm-pairing", "srm-recovery-plans", "srm-protection-groups", "srm-issues"]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert len({group.group_key for group in groups}) == 4
    assert all(group.when_to_use for group in groups)
