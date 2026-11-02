# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX migration operations, with a respx-mocked HCX Manager and no ingest.

* every op is registered typed, with the safety level and approval flag of
  the Task table;
* every change waits for approval and calls no vendor system until then;
* validate, start, list, cancel, mobility group list and set, and the
  switchover window send the right method, path and body, and return the
  documented fields;
* the vendor jobs: a start returns ``accepted`` with the ids and the status
  read; a running migration of the same group or VMs stops a second start;
  a failed migration shows the vendor error; a login that expires between
  start and status logs in again; HCX that does not answer gives
  ``reachable: false`` with the ids and the time; a start of VMs that fails
  at its second step lists what ran, what failed and what did not run;
* a vendor error reaches the answer.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.hcx.migration_body import group_item, migration_outcome
from meho_backplane.connectors.hcx.migration_ops import GROUP_MIGRATION, HCX_MIGRATION_OPS
from meho_backplane.connectors.hcx.typed_ops import HCX_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_HOST = "hcx-migrations.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSIONS = "/hybridity/api/sessions"
_MIGRATIONS = "/hybridity/api/migrations"
_GROUPS = "/hybridity/api/mobility/groups"
_GROUPS_START = "/hybridity/api/mobility/groups/start"
_GROUPS_VALIDATE = "/hybridity/api/mobility/groups/validate"
_CANCEL = "/hybridity/api/mobility/migrations/cancel"
_FORCE_CANCEL = "/hybridity/api/mobility/migrations/forceCancel"
_TOKEN = "migrations-hm-token-1"
_PASSWORD = "migrations-password-value"
_GROUP_ID = "group-0001"
_MIGRATION_ID = "migration-0001"
_VM_ID = "vm-0001"
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000d5")

#: The Task table: op id -> (safety level, requires approval).
_RISK = {
    "hcx.migration.validate": ("safe", False),
    "hcx.migration.start": ("dangerous", True),
    "hcx.migration.list": ("safe", False),
    "hcx.migration.cancel": ("dangerous", True),
    "hcx.mobility_group.list": ("safe", False),
    "hcx.mobility_group.set": ("caution", True),
    "hcx.migration.schedule": ("dangerous", True),
}

_GROUP_INPUT: dict[str, Any] = {
    "name": "<group-name>",
    "migration_type": "rav",
    "service_mesh_id": "<mesh-id>",
    "source": {"endpoint_id": "<source-site-id>", "resource_id": "<source-vcenter-id>"},
    "destination": {
        "endpoint_id": "<target-site-id>",
        "resource_id": "<target-vcenter-id>",
        "resource_name": "<target-vcenter>",
    },
    "placement": [{"id": "<folder-id>", "type": "folder"}],
    "storage": {"datastore_id": "<datastore-id>", "disk_provision_type": "thin"},
    "network_mappings": [
        {"source_network_id": "<source-net-id>", "destination_network_id": "<target-net-id>"}
    ],
    "switchover_options": {"retain_mac": True},
    "vms": [{"vm_id": _VM_ID, "vm_name": "<vm-name>"}],
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
        product=HCX_PRODUCT, version=HCX_VERSION, impl_id=HCX_IMPL_ID, cls=HcxConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


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
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def registered(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await register_hcx_typed_operations()
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]


class _HcxTarget:
    """Target satisfying both ``HcxTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = HCX_PRODUCT
        self.fingerprint = type("_FP", (), {"version": HCX_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "hcx-migrations"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-migrations/hcx-migrations"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-migrations-hcx-typed",
        name="HCX Migrations Operator",
        email=None,
        raw_jwt="op.migrations.hcx.typed.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-migrations-svc", "password": _PASSWORD}


async def _call(op_id: str, params: dict[str, Any], *, approved: bool = False) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id=op_id,
        target=_HcxTarget(),
        params=params,
        _approved=approved,
    )


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_SESSIONS).respond(200, headers={"x-hm-authorization": _TOKEN})


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


def _migration(vendor_state: str, **progress: Any) -> dict[str, Any]:
    return {
        "migrationId": _MIGRATION_ID,
        "migrationGroupId": _GROUP_ID,
        "migrationType": "RAV",
        "state": vendor_state,
        "entity": {"entityId": _VM_ID, "entityName": "<vm-name>"},
        "progress": progress,
    }


def _query_route(mock: respx.MockRouter, rows: list[dict[str, Any]]) -> respx.Route:
    return mock.post(_MIGRATIONS, params={"action": "query"}).respond(
        200, json={"data": {"items": rows}}
    )


def _group_answer(group_id: str = _GROUP_ID, **extra: Any) -> dict[str, Any]:
    return {
        "items": [
            {
                "migrationGroupId": group_id,
                "name": "<group-name>",
                "state": "DRAFT",
                "migrations": [
                    {"migrationId": _MIGRATION_ID, "entityId": _VM_ID, "errors": [], "warnings": []}
                ],
                **extra,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Registration, risk and approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ops_are_registered_typed_with_the_task_risk(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_hcx_typed_operations()
    rows = {
        row.op_id: row
        for row in (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == HCX_PRODUCT,
                    EndpointDescriptor.impl_id == HCX_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    }
    assert {op.op_id for op in HCX_MIGRATION_OPS} == set(_RISK)
    for op_id, (safety_level, requires_approval) in _RISK.items():
        row = rows[op_id]
        assert row.source_kind == "typed", op_id
        assert row.safety_level == safety_level, op_id
        assert row.requires_approval is requires_approval, op_id
        assert row.parameter_schema["examples"], op_id
        assert "\n" not in row.summary and len(row.description) <= 300, op_id
    assert GROUP_MIGRATION in HCX_TYPED_WHEN_TO_USE_BY_GROUP
    group_text = HCX_TYPED_WHEN_TO_USE_BY_GROUP[GROUP_MIGRATION]
    for op_id in _RISK:
        assert op_id in group_text


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("hcx.migration.start", {"mobility_group_id": _GROUP_ID}),
        ("hcx.migration.cancel", {"migrations": [{"migration_id": _MIGRATION_ID}]}),
        ("hcx.mobility_group.set", {"group": _GROUP_INPUT}),
        (
            "hcx.migration.schedule",
            {"mobility_group_id": _GROUP_ID, "switchover": {"clear": True}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_change_waits_for_approval(
    registered: None, op_id: str, params: dict[str, Any]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call(op_id, params)

    assert result.status == "awaiting_approval", result
    assert not login.called


# ---------------------------------------------------------------------------
# hcx.migration.validate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_a_group_by_id(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(_GROUPS_VALIDATE).respond(200, json=_group_answer())
        result = await _call("hcx.migration.validate", {"mobility_group_id": _GROUP_ID})

    assert result.status == "ok", result.error
    assert _body(route) == {"items": [{"migrationGroupId": _GROUP_ID}]}
    assert route.calls[0].request.headers["x-hm-authorization"] == _TOKEN
    assert result.result["valid"] is True
    assert result.result["mobility_group_id"] == _GROUP_ID
    assert result.result["migrations"][0]["migration_id"] == _MIGRATION_ID


@pytest.mark.asyncio
async def test_validate_a_planned_group_returns_the_vendor_errors(registered: None) -> None:
    failed = {
        "items": [
            {
                "name": "<group-name>",
                "errors": [{"code": "E1", "message": "datastore has no free space"}],
                "migrations": [{"entityId": _VM_ID, "errors": [{"message": "network not mapped"}]}],
            }
        ]
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(_GROUPS_VALIDATE).respond(400, json=failed)
        result = await _call("hcx.migration.validate", {"group": _GROUP_INPUT})

    assert result.status == "ok", result.error
    assert _body(route) == {"items": [group_item(_GROUP_INPUT)]}
    assert result.result["valid"] is False
    assert result.result["errors"][0]["message"] == "datastore has no free space"
    assert result.result["migrations"][0]["errors"][0]["message"] == "network not mapped"


def test_group_item_maps_the_input_to_the_vendor_group() -> None:
    item = group_item(_GROUP_INPUT)
    defaults = item["groupDefaults"]
    assert item["name"] == "<group-name>"
    assert defaults["migrationType"] == "RAV"
    assert defaults["transferParams"] == {"transferType": "vSphereReplication"}
    assert defaults["switchoverParams"] == {"switchoverType": "RAV", "options": {"retainMac": True}}
    assert defaults["servicemeshId"] == "<mesh-id>"
    assert defaults["source"] == {
        "endpointId": "<source-site-id>",
        "resourceId": "<source-vcenter-id>",
        "endpointType": "VC",
        "resourceType": "VC",
    }
    assert defaults["destination"]["resourceName"] == "<target-vcenter>"
    assert defaults["placement"] == [{"id": "<folder-id>", "type": "folder"}]
    assert defaults["storage"] == {
        "defaultStorage": {"id": "<datastore-id>", "type": "datastore", "diskProvisionType": "thin"}
    }
    assert defaults["networkParams"] == {
        "defaultMappings": [{"srcNetworkId": "<source-net-id>", "destNetworkId": "<target-net-id>"}]
    }
    assert item["migrations"] == [
        {
            "migrationType": "RAV",
            "entity": {
                "entityId": _VM_ID,
                "entityType": "VirtualMachine",
                "entityName": "<vm-name>",
            },
        }
    ]


@pytest.mark.parametrize(
    ("migration_type", "transfer", "switchover"),
    [
        ("bulk", "vSphereReplication", "vSphereReplication"),
        ("vmotion", "NO_OP", "vMotion"),
        ("rav", "vSphereReplication", "RAV"),
        ("cold", "NO_OP", "ColdMigration"),
    ],
)
def test_group_item_maps_each_migration_type(
    migration_type: str, transfer: str, switchover: str
) -> None:
    item = group_item({**_GROUP_INPUT, "migration_type": migration_type})
    assert item["groupDefaults"]["transferParams"]["transferType"] == transfer
    assert item["groupDefaults"]["switchoverParams"]["switchoverType"] == switchover
    assert item["migrations"][0]["migrationType"] == switchover


# ---------------------------------------------------------------------------
# hcx.migration.start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_a_group_returns_accepted_with_the_ids_and_the_status_read(
    registered: None,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _query_route(mock, [])
        start = mock.post(_GROUPS_START).respond(202, json=_group_answer())
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "ok", result.error
    assert _body(query) == {"filter": {"skipDrafts": True, "migrationGroupId": [_GROUP_ID]}}
    assert _body(start) == {"items": [{"migrationGroupId": _GROUP_ID}]}
    answer = result.result
    assert answer["status"] == "accepted"
    assert answer["mobility_group_id"] == _GROUP_ID
    assert answer["migrations"][0]["migration_id"] == _MIGRATION_ID
    assert answer["status_read"] == "hcx.migration.list"
    assert answer["status_read_params"] == {"mobility_group_id": _GROUP_ID}
    assert answer["steps"] == [{"step": "start_group", "state": "done", "error": None}]


@pytest.mark.asyncio
async def test_start_does_not_start_again_while_a_migration_runs(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [_migration("TRANSFER_IN_PROGRESS", isDone=False)])
        start = mock.post(_GROUPS_START).respond(202, json=_group_answer())
        create = mock.post(_GROUPS).respond(201, json=_group_answer())
        by_group = await _call(
            "hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True
        )
        by_vms = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)

    for result in (by_group, by_vms):
        assert result.status == "ok", result.error
        assert result.result["status"] == "already_running"
        assert result.result["running"] == [
            {
                "migration_id": _MIGRATION_ID,
                "vm_id": _VM_ID,
                "state": "TRANSFER_IN_PROGRESS",
                "outcome": "running",
            }
        ]
    assert not start.called
    assert not create.called


@pytest.mark.asyncio
async def test_already_running_status_read_params_work_with_the_status_read(
    registered: None,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _query_route(mock, [_migration("TRANSFER_IN_PROGRESS", isDone=False)])
        by_group = await _call(
            "hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True
        )
        by_vms = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)
        group_status = await _call("hcx.migration.list", by_group.result["status_read_params"])
        vms_status = await _call("hcx.migration.list", by_vms.result["status_read_params"])

    assert by_group.result["status_read_params"] == {
        "mobility_group_id": _GROUP_ID,
        "migration_ids": [_MIGRATION_ID],
    }
    assert by_vms.result["status_read_params"] == {"migration_ids": [_MIGRATION_ID]}
    for status in (group_status, vms_status):
        assert status.status == "ok", status.error
        assert status.result["items"][0]["migration_id"] == _MIGRATION_ID
    assert _body(query, 3) == {"filter": {"skipDrafts": True, "migrationId": [_MIGRATION_ID]}}


@pytest.mark.asyncio
async def test_start_vms_without_a_group_id_follows_the_vms(registered: None) -> None:
    error = {"items": [{"errors": [{"message": "name is taken"}]}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _query_route(mock, [])
        mock.post(_GROUPS).respond(200, json=error)
        start = mock.post(_GROUPS_START).respond(202, json=_group_answer())
        result = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)
        status = await _call("hcx.migration.list", result.result["status_read_params"])

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "failed"
    assert answer["steps"] == [
        {"step": "create_group", "state": "failed", "error": "name is taken"},
        {"step": "start_group", "state": "not_run", "error": None},
    ]
    assert answer["status_read_params"] == {"vm_ids": [_VM_ID]}
    assert not start.called
    assert status.status == "ok", status.error
    assert _body(query, 1) == {"filter": {"skipDrafts": True, "entityId": [_VM_ID]}}


@pytest.mark.asyncio
async def test_start_vms_makes_a_group_and_starts_it(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _query_route(mock, [_migration("MIGRATED", isDone=True)])
        create = mock.post(_GROUPS).respond(201, json=_group_answer())
        start = mock.post(_GROUPS_START).respond(202, json=_group_answer())
        result = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "ok", result.error
    assert _body(query) == {"filter": {"skipDrafts": True, "entityId": [_VM_ID]}}
    assert _body(create) == {"items": [group_item(_GROUP_INPUT)]}
    assert _body(start) == {"items": [{"migrationGroupId": _GROUP_ID}]}
    assert result.result["status"] == "accepted"
    assert [step["state"] for step in result.result["steps"]] == ["done", "done"]


@pytest.mark.asyncio
async def test_start_vms_failing_at_step_two_lists_what_ran_and_what_failed(
    registered: None,
) -> None:
    error = {"errors": [{"code": "E2", "message": "service mesh is down"}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS).respond(201, json=_group_answer())
        mock.post(_GROUPS_START).respond(500, json=error)
        result = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "failed"
    assert answer["mobility_group_id"] == _GROUP_ID
    assert answer["steps"][0] == {"step": "create_group", "state": "done", "error": None}
    assert answer["steps"][1]["step"] == "start_group"
    assert answer["steps"][1]["state"] == "failed"
    assert "service mesh is down" in answer["steps"][1]["error"]
    assert answer["status_read_params"] == {"mobility_group_id": _GROUP_ID}


@pytest.mark.asyncio
async def test_start_vms_with_no_answer_at_step_two_says_no_answer(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS).respond(201, json=_group_answer())
        mock.post(_GROUPS_START).mock(side_effect=httpx.ReadTimeout("no reply"))
        result = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "ok", result.error
    assert result.result["status"] == "no_answer"
    assert result.result["steps"][1]["state"] == "failed"
    assert "no answer from HCX" in result.result["steps"][1]["error"]


@pytest.mark.asyncio
async def test_start_of_a_group_vendor_error_reaches_the_answer(registered: None) -> None:
    error = {"items": [{"errors": [{"message": "group is not valid"}]}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS_START).respond(400, json=error)
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "error"
    assert "group is not valid" in json.dumps(result.extras, default=str)


_ECHOED_SECRET = "echoed-secret-value-0001"


@pytest.mark.parametrize(
    "body",
    [
        {"password": _ECHOED_SECRET, "detail": "login failed"},
        {"errors": [{"message": f'login failed with "password": "{_ECHOED_SECRET}"'}]},
        {"errors": [{"message": f"token Bearer {_ECHOED_SECRET} was refused"}]},
    ],
    ids=["secret-field-in-body", "secret-field-in-message", "bearer-token-in-message"],
)
@pytest.mark.asyncio
async def test_start_step_error_holds_no_secret_from_the_vendor_body(
    registered: None, body: dict[str, Any]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS).respond(201, json=_group_answer())
        mock.post(_GROUPS_START).respond(500, json=body)
        result = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "ok", result.error
    step = result.result["steps"][1]
    assert step["state"] == "failed"
    assert step["error"].startswith("HTTP 500: ")
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert _ECHOED_SECRET not in rendered
    assert _PASSWORD not in rendered
    assert _TOKEN not in rendered


@pytest.mark.asyncio
async def test_start_of_a_group_rejected_with_http_200_says_failed(registered: None) -> None:
    answer = _group_answer(errors=[{"code": "E1", "message": "group is not valid"}])
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS_START).respond(200, json=answer)
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "failed"
    assert answer["errors"] == [{"code": "E1", "message": "group is not valid"}]
    assert answer["steps"] == [
        {"step": "start_group", "state": "failed", "error": "group is not valid"}
    ]
    assert answer["status_read_params"] == {"mobility_group_id": _GROUP_ID}


@pytest.mark.asyncio
async def test_start_vms_rejected_with_http_200_keeps_the_group(registered: None) -> None:
    rejected = {
        "migrationId": _MIGRATION_ID,
        "entityId": _VM_ID,
        "errors": [{"message": "no seed datastore"}],
        "warnings": [],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS).respond(201, json=_group_answer())
        mock.post(_GROUPS_START).respond(200, json=_group_answer(migrations=[rejected]))
        result = await _call("hcx.migration.start", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "failed"
    assert answer["mobility_group_id"] == _GROUP_ID
    assert answer["migrations"][0]["errors"] == [{"message": "no seed datastore"}]
    assert answer["steps"] == [
        {"step": "create_group", "state": "done", "error": None},
        {"step": "start_group", "state": "failed", "error": f"{_VM_ID}: no seed datastore"},
    ]
    assert answer["status_read_params"] == {"mobility_group_id": _GROUP_ID}


@pytest.mark.asyncio
async def test_start_with_some_migrations_rejected_says_partial(registered: None) -> None:
    started = {"migrationId": _MIGRATION_ID, "entityId": _VM_ID, "errors": [], "warnings": []}
    rejected = {
        "migrationId": "migration-0002",
        "entityId": "vm-0002",
        "errors": [{"message": "VM is powered off"}],
        "warnings": [],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS_START).respond(200, json=_group_answer(migrations=[started, rejected]))
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "partial"
    assert [m["migration_id"] for m in answer["migrations"]] == [_MIGRATION_ID, "migration-0002"]
    assert answer["migrations"][0]["errors"] == []
    assert answer["migrations"][1]["errors"] == [{"message": "VM is powered off"}]
    assert answer["steps"] == [
        {"step": "start_group", "state": "partial", "error": "vm-0002: VM is powered off"}
    ]
    assert answer["status_read_params"] == {"mobility_group_id": _GROUP_ID}


@pytest.mark.asyncio
async def test_start_of_a_group_rejected_with_only_a_code_says_failed(registered: None) -> None:
    answer = _group_answer(errors=[{"code": "E1"}])
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS_START).respond(200, json=answer)
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "failed"
    assert answer["errors"] == [{"code": "E1"}]
    assert answer["steps"] == [{"step": "start_group", "state": "failed", "error": "E1"}]


@pytest.mark.asyncio
async def test_start_with_a_migration_rejected_without_a_message_says_partial(
    registered: None,
) -> None:
    started = {"migrationId": _MIGRATION_ID, "entityId": _VM_ID, "errors": [], "warnings": []}
    rejected = {"migrationId": "migration-0002", "entityId": "vm-0002", "errors": [{}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS_START).respond(200, json=_group_answer(migrations=[started, rejected]))
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["status"] == "partial"
    assert answer["migrations"][1]["errors"] == [{}]
    assert answer["steps"] == [
        {"step": "start_group", "state": "partial", "error": "vm-0002: rejected"}
    ]


_SECRET_MESSAGE = f'login failed with "password": "{_ECHOED_SECRET}"'


@pytest.mark.parametrize(
    ("answer", "copies"),
    [
        (
            _group_answer(errors=[{"code": "E1", "message": _SECRET_MESSAGE}]),
            lambda answer: [answer["errors"][0]["message"]],
        ),
        (
            _group_answer(
                migrations=[
                    {
                        "migrationId": _MIGRATION_ID,
                        "entityId": _VM_ID,
                        "errors": [{"message": _SECRET_MESSAGE}],
                        "warnings": [{"message": f"token Bearer {_ECHOED_SECRET} expires soon"}],
                    }
                ]
            ),
            lambda answer: [
                answer["migrations"][0]["errors"][0]["message"],
                answer["migrations"][0]["warnings"][0]["message"],
            ],
        ),
    ],
    ids=["group-rejection", "migration-rejection"],
)
@pytest.mark.asyncio
async def test_a_rejection_with_http_200_holds_no_secret_in_any_copy(
    registered: None, answer: dict[str, Any], copies: Callable[[dict[str, Any]], list[str]]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [])
        mock.post(_GROUPS_START).respond(200, json=answer)
        result = await _call("hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True)

    assert result.status == "ok", result.error
    assert result.result["status"] == "failed"
    assert result.result["steps"][-1]["state"] == "failed"
    for copy in copies(result.result):
        assert "login failed" in copy or "expires soon" in copy
        assert _ECHOED_SECRET not in copy
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert _ECHOED_SECRET not in rendered


# ---------------------------------------------------------------------------
# hcx.migration.list -- the status read
# ---------------------------------------------------------------------------


def test_migration_outcome_reads_the_flags_then_the_state() -> None:
    assert migration_outcome({"progress": {"isDone": False}}) == "running"
    assert migration_outcome({"progress": {"isDone": True}}) == "succeeded"
    assert migration_outcome({"progress": {"isDone": True, "didFail": True}}) == "failed"
    assert migration_outcome({"progress": {"isDone": True, "isCancelled": True}}) == "cancelled"
    assert migration_outcome({"state": "MIGRATE_FAILED"}) == "failed"
    assert migration_outcome({"state": "MIGRATED"}) == "succeeded"
    assert migration_outcome({"state": "<other>"}) == "unknown"


@pytest.mark.asyncio
async def test_list_filters_and_returns_flat_rows_with_the_vendor_error(registered: None) -> None:
    failed = _migration(
        "MIGRATE_FAILED",
        isDone=True,
        didFail=True,
        startTime=1000,
        endTime=2000,
        state={"message": "switchover", "errorMessage": "disk copy failed"},
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _query_route(mock, [failed])
        result = await _call(
            "hcx.migration.list",
            {
                "state": ["MIGRATE_FAILED"],
                "migration_ids": [_MIGRATION_ID],
                "vm_ids": [_VM_ID],
                "mobility_group_id": _GROUP_ID,
            },
        )

    assert result.status == "ok", result.error
    assert _body(query) == {
        "filter": {
            "skipDrafts": True,
            "migrationId": [_MIGRATION_ID],
            "entityId": [_VM_ID],
            "state": ["MIGRATE_FAILED"],
            "migrationGroupId": [_GROUP_ID],
        }
    }
    row = result.result["items"][0]
    assert row["migration_id"] == _MIGRATION_ID
    assert row["mobility_group_id"] == _GROUP_ID
    assert row["vm_id"] == _VM_ID
    assert row["vm_name"] == "<vm-name>"
    assert row["outcome"] == "failed"
    assert row["error_message"] == "disk copy failed"
    assert row["status_message"] == "switchover"
    assert (row["start_time"], row["end_time"]) == (1000, 2000)
    assert result.result["reachable"] is True


@pytest.mark.asyncio
async def test_list_says_not_reachable_with_the_ids_and_the_time(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_MIGRATIONS).mock(side_effect=httpx.ConnectError("connection refused"))
        result = await _call("hcx.migration.list", {"migration_ids": [_MIGRATION_ID]})

    assert result.status == "ok", result.error
    assert result.result["reachable"] is False
    assert result.result["items"] == []
    assert result.result["migration_ids"] == [_MIGRATION_ID]
    checked_at = datetime.fromisoformat(result.result["checked_at"])
    assert abs((datetime.now(UTC) - checked_at).total_seconds()) < 60


@pytest.mark.asyncio
async def test_list_logs_in_again_when_the_login_expired_after_the_start(
    registered: None,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSIONS)
        login.side_effect = [
            httpx.Response(200, headers={"x-hm-authorization": "migrations-hm-token-1"}),
            httpx.Response(200, headers={"x-hm-authorization": "migrations-hm-token-2"}),
        ]
        query = mock.post(_MIGRATIONS)
        query.side_effect = [
            httpx.Response(200, json={"data": {"items": []}}),
            httpx.Response(401),
            httpx.Response(200, json={"data": {"items": [_migration("RUNNING", isDone=False)]}}),
        ]
        mock.post(_GROUPS_START).respond(202, json=_group_answer())
        started = await _call(
            "hcx.migration.start", {"mobility_group_id": _GROUP_ID}, approved=True
        )
        status = await _call("hcx.migration.list", started.result["status_read_params"])

    assert started.status == "ok", started.error
    assert status.status == "ok", status.error
    assert login.call_count == 2
    assert query.calls[2].request.headers["x-hm-authorization"] == "migrations-hm-token-2"
    assert status.result["items"][0]["outcome"] == "running"


@pytest.mark.asyncio
async def test_long_migration_list_goes_behind_a_result_handle(registered: None) -> None:
    set_default_reducer(JsonFluxReducer())
    rows = [{**_migration("MIGRATED", isDone=True), "migrationId": f"m-{i}"} for i in range(120)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, rows)
        result = await _call("hcx.migration.list", {})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 120


@pytest.mark.asyncio
async def test_results_hold_no_password_or_token(registered: None) -> None:
    row = {**_migration("MIGRATED", isDone=True), "vcPassword": "made-up-password"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [row])
        result = await _call("hcx.migration.list", {})

    assert result.status == "ok", result.error
    # The connector masks the password; the default view of every answer
    # hides the ``vcPassword`` field once more.
    assert result.result["items"][0]["vcPassword"] == "[REDACTED:param_name]"
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _TOKEN, "made-up-password"):
        assert secret not in rendered


# ---------------------------------------------------------------------------
# hcx.migration.cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("force", "path"), [(False, _CANCEL), (True, _FORCE_CANCEL)])
@pytest.mark.asyncio
async def test_cancel_sends_only_the_migrations_that_still_run(
    registered: None, force: bool, path: str
) -> None:
    running = {**_migration("TRANSFER_IN_PROGRESS", isDone=False), "migrationId": "m-run"}
    ended = {**_migration("MIGRATED", isDone=True), "migrationId": "m-done"}
    answer = {"items": [{"migrationId": "m-run", "migrationGroupId": _GROUP_ID, "errors": []}]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        query = _query_route(mock, [running, ended])
        cancel = mock.post(path).respond(202, json=answer)
        result = await _call(
            "hcx.migration.cancel",
            {
                "migrations": [
                    {"migration_id": "m-run", "mobility_group_id": _GROUP_ID},
                    {"migration_id": "m-done"},
                ],
                "force": force,
            },
            approved=True,
        )

    assert result.status == "ok", result.error
    assert _body(query)["filter"]["migrationId"] == ["m-run", "m-done"]
    assert _body(cancel) == {"items": [{"migrationId": "m-run", "migrationGroupId": _GROUP_ID}]}
    assert result.result["status"] == "accepted"
    assert result.result["force"] is force
    assert result.result["migrations"][0]["migration_id"] == "m-run"
    assert result.result["already_ended"] == [{"migration_id": "m-done", "outcome": "succeeded"}]
    assert result.result["status_read"] == "hcx.migration.list"
    assert result.result["status_read_params"] == {"migration_ids": ["m-run", "m-done"]}


@pytest.mark.asyncio
async def test_cancel_of_ended_migrations_sends_nothing(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [_migration("MIGRATE_CANCELED", isDone=True, isCancelled=True)])
        cancel = mock.post(_CANCEL).respond(202, json={"items": []})
        result = await _call(
            "hcx.migration.cancel", {"migrations": [{"migration_id": _MIGRATION_ID}]}, approved=True
        )

    assert result.status == "ok", result.error
    assert result.result["status"] == "already_ended"
    assert not cancel.called


@pytest.mark.asyncio
async def test_cancel_vendor_error_reaches_the_answer(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        _query_route(mock, [_migration("TRANSFER_IN_PROGRESS", isDone=False)])
        mock.post(_CANCEL).respond(500, json={"errors": [{"message": "cancel not allowed now"}]})
        result = await _call(
            "hcx.migration.cancel", {"migrations": [{"migration_id": _MIGRATION_ID}]}, approved=True
        )

    assert result.status == "error"
    assert "cancel not allowed now" in json.dumps(result.extras, default=str)


# ---------------------------------------------------------------------------
# hcx.mobility_group.list and hcx.mobility_group.set
# ---------------------------------------------------------------------------


_GROUP_INFO = {
    "info": {"id": _GROUP_ID, "name": "<group-name>", "servicemeshId": "<mesh-id>"},
    "summary": {"state": "READY", "configurationStatus": "VALID", "isArchived": False},
}


@pytest.mark.asyncio
async def test_mobility_group_list_queries_the_groups(registered: None) -> None:
    page = {"pageSize": 10, "pageNumber": 0, "totalPages": 1, "totalElements": 1}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post("/hybridity/api/v2/mobility-groups/query").respond(
            200, json={"items": [_GROUP_INFO], "pageParameters": page}
        )
        result = await _call(
            "hcx.mobility_group.list", {"name": "<group-name>", "state": ["READY"], "page_size": 10}
        )

    assert result.status == "ok", result.error
    assert _body(route) == {
        "filter": {"mobilityGroupName": "<group-name>", "mobilityGroupState": ["READY"]},
        "pageParameters": {"pageSize": 10},
    }
    row = result.result["items"][0]
    assert row["mobility_group_id"] == _GROUP_ID
    assert (row["state"], row["configuration_status"]) == ("READY", "VALID")
    assert row["service_mesh_id"] == "<mesh-id>"
    assert result.result["page"] == page


@pytest.mark.asyncio
async def test_mobility_group_list_reads_one_group_summary(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(f"/hybridity/api/v2/mobility-groups/{_GROUP_ID}/summary").respond(
            200, json={"item": _GROUP_INFO}
        )
        result = await _call("hcx.mobility_group.list", {"mobility_group_id": _GROUP_ID})

    assert result.status == "ok", result.error
    assert route.called
    assert result.result["items"][0]["name"] == "<group-name>"


@pytest.mark.asyncio
async def test_mobility_group_set_makes_a_new_group(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(_GROUPS).respond(201, json=_group_answer())
        result = await _call("hcx.mobility_group.set", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "ok", result.error
    assert _body(route) == {"items": [group_item(_GROUP_INPUT)]}
    assert result.result["created"] is True
    assert result.result["mobility_group_id"] == _GROUP_ID
    assert result.result["migrations"][0]["vm_id"] == _VM_ID


@pytest.mark.asyncio
async def test_mobility_group_set_changes_a_group_with_put(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.put(_GROUPS).respond(200, json=_group_answer())
        result = await _call(
            "hcx.mobility_group.set",
            {"mobility_group_id": _GROUP_ID, "group": _GROUP_INPUT},
            approved=True,
        )

    assert result.status == "ok", result.error
    assert _body(route) == {"items": [{**group_item(_GROUP_INPUT), "migrationGroupId": _GROUP_ID}]}
    assert result.result["created"] is False


@pytest.mark.asyncio
async def test_mobility_group_set_vendor_error_reaches_the_answer(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_GROUPS).respond(409, json={"errors": [{"message": "name already used"}]})
        result = await _call("hcx.mobility_group.set", {"group": _GROUP_INPUT}, approved=True)

    assert result.status == "error"
    assert "name already used" in json.dumps(result.extras, default=str)


# ---------------------------------------------------------------------------
# hcx.migration.schedule
# ---------------------------------------------------------------------------


_SCHEDULE = f"/hybridity/api/mobility/groups/{_GROUP_ID}/schedule"


@pytest.mark.asyncio
async def test_schedule_sends_the_window_in_epoch_milliseconds(registered: None) -> None:
    answer = {"affectedTransferScheduleCount": 0, "affectedSwitchoverScheduleCount": 2}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(_SCHEDULE).respond(200, json=answer)
        result = await _call(
            "hcx.migration.schedule",
            {
                "mobility_group_id": _GROUP_ID,
                "switchover": {
                    "start_time": "2030-01-01T22:00:00Z",
                    "end_time": "2030-01-02T02:00:00+00:00",
                },
            },
            approved=True,
        )

    assert result.status == "ok", result.error
    assert _body(route) == {
        "mobilityGroupDefaults": {
            "switchover": {"startTime": 1893535200000, "expiryTime": 1893549600000}
        }
    }
    assert result.result["affected_switchover_schedules"] == 2
    assert result.result["errors"] == []


@pytest.mark.asyncio
async def test_schedule_clears_the_window_and_returns_the_vendor_errors(registered: None) -> None:
    answer = {
        "errors": {
            "migrations": [{"id": _MIGRATION_ID, "errors": [{"code": "S1", "message": "too late"}]}]
        }
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(_SCHEDULE).respond(200, json=answer)
        result = await _call(
            "hcx.migration.schedule",
            {"mobility_group_id": _GROUP_ID, "switchover": {"clear": True}},
            approved=True,
        )

    assert result.status == "ok", result.error
    assert _body(route) == {"mobilityGroupDefaults": {"switchover": {"clearSchedule": True}}}
    assert result.result["errors"] == [
        {"scope": "migration", "id": _MIGRATION_ID, "code": "S1", "message": "too late"}
    ]


@pytest.mark.parametrize(
    "window",
    [
        {"start_time": "2030-01-01T22:00:00", "end_time": "2030-01-02T02:00:00"},
        {"start_time": "2030-01-02T02:00:00Z", "end_time": "2030-01-01T22:00:00Z"},
    ],
)
@pytest.mark.asyncio
async def test_schedule_refuses_a_window_without_time_zone_or_backwards(
    registered: None, window: dict[str, str]
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(_SCHEDULE).respond(200, json={})
        result = await _call(
            "hcx.migration.schedule",
            {"mobility_group_id": _GROUP_ID, "switchover": window},
            approved=True,
        )

    assert result.status == "error"
    assert not route.called


@pytest.mark.asyncio
async def test_schedule_vendor_error_reaches_the_answer(registered: None) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post(_SCHEDULE).respond(404, json={"errors": [{"message": "group not found"}]})
        result = await _call(
            "hcx.migration.schedule",
            {"mobility_group_id": _GROUP_ID, "switchover": {"clear": True}},
            approved=True,
        )

    assert result.status == "error"
    assert "group not found" in json.dumps(result.extras, default=str)
