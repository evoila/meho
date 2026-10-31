# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vSphere Replication pairing, replication, VM-check and task reads.

``vr.pairing.list``, ``vr.replication.list``, ``vr.replication.issues``,
``vr.vm.check`` and ``vr.task.get``, run through ``dispatch()`` against a
respx-mocked appliance with no ingested catalog. Covered: the vendor
paths and query (``extended_info``, ``limit`` / ``offset`` pages, the
name filter), flat rows, the RPO fields, partial data in ``errors``, the
disk read limit, the target datastores of another vCenter, secret names in
a task result, a vendor error, a ``..`` id, a ``401`` that logs in again,
and the registered metadata.

Fixture values are made up; no real host, user or id appears here.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vr import (
    VR_CONNECTOR_ID,
    VR_IMPL_ID,
    VR_PRODUCT,
    VR_TYPED_OPS,
    VR_VERSION,
    VrConnector,
    register_vr_typed_operations,
)
from meho_backplane.connectors.vr.reads import MAX_DISK_READS, PAGE_SIZE, REDACTED
from meho_backplane.connectors.vr.typed_ops import VR_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_HOST = "vr-replication.test.invalid"
_BASE_URL = f"https://{_HOST}"
_API = "/api/rest/vr/v2"
_SESSION = f"{_API}/session"
_PAIRINGS = f"{_API}/pairings"
_PAIRING = "pairing-0001"
_PASSWORD = "replication-password-value"
_NEW_OPS = {
    "vr.pairing.list",
    "vr.replication.list",
    "vr.replication.issues",
    "vr.vm.check",
    "vr.task.get",
}


def _server(name: str, server_id: str) -> dict[str, Any]:
    return {
        "id": server_id,
        "url": f"https://<{name}>:443",
        "name": f"<{name}>",
        "server_status": "OK",
        "version": "9.1.1.0",
        "build_number": "22222222",
        "certificates": [{"certificate": "made-up-pem"}],
    }


_PAIRING_PAYLOAD: dict[str, Any] = {
    "list": [
        {
            "pairing_id": _PAIRING,
            "local_vc_server": _server("local-vcenter", "vc-local-0001"),
            "remote_vc_server": _server("remote-vcenter", "vc-remote-0001"),
            "local_vrms": _server("local-vr", "vrms-local-0001"),
            "remote_vrms": _server("remote-vr", "vrms-remote-0001"),
        }
    ],
    "_meta": {"total": 1, "errors": []},
}
_VRS_PAYLOAD: dict[str, Any] = {
    "list": [
        {"id": "vrms-local-0001", "name": "<local-vr>"},
        {"id": "vrms-remote-0001", "name": "<remote-vr>"},
    ],
    "_meta": {"errors": []},
}
_SERVERS_PAYLOAD: dict[str, Any] = {
    "list": [
        {
            "id": "vrs-0001",
            "name": "<replication-server>",
            "hostname": "<replication-server-host>",
            "uuid": "vrs-uuid-0001",
            "connected": True,
            "decommissioned": False,
            "internally_managed": True,
            "replication_count": 3,
            "fault": None,
        }
    ],
    "_meta": {"total": 1, "errors": []},
}


def _replication(index: int, *, rpo_violation: bool = False) -> dict[str, Any]:
    return {
        "id": f"replication-{index:04d}",
        "name": f"<vm-{index}>",
        "vm_id": f"vm-{index}",
        "source_site_name": "<source-site>",
        "source_vc_id": "vc-local-0001",
        "target_site_name": "<target-site>",
        "target_vc_id": "vc-remote-0001",
        "status": {
            "status": "RPO_VIOLATION" if rpo_violation else "OK",
            "rpo_violation": rpo_violation,
        },
        "rpo": 15,
        "type": "VC_TO_VC",
        "configuration_state": "OK",
        "configuration_error": {"message": None, "details": None},
        "last_group_error": {"message": "made-up error", "details": "made-up details"},
        "recovery_state": "NOT_STARTED",
        "last_sync_time": 1700000000000,
        "last_sync_duration": 12,
        "last_sync_size": 4096,
        "current_rpo_violation": 30 if rpo_violation else 0,
        "mpit_enabled": False,
        "sync_progress": {"progress": 100},
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
        product=VR_PRODUCT, version=VR_VERSION, impl_id=VR_IMPL_ID, cls=VrConnector
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


class _Target:
    """Target satisfying the connector Protocols and the resolver shape."""

    def __init__(self) -> None:
        self.product = VR_PRODUCT
        self.fingerprint = type("_FP", (), {"version": VR_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = f"vr-replication-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/vr-replication"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-reads-vr-replication",
        name="VR Replication Reads Operator",
        email=None,
        raw_jwt="op.reads.vr.replication.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "vr-replication-svc", "password": _PASSWORD}


@pytest.fixture
async def _connector(_stub_embedding: AsyncMock, session: AsyncSession) -> VrConnector:
    await register_vr_typed_operations()
    instance = get_or_create_connector_instance(VrConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, VrConnector)
    return instance


def _login(session_id: str = "vr-session-1") -> httpx.Response:
    return httpx.Response(200, json={"session_id": session_id})


async def _dispatch(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=VR_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=params,
    )


# ---------------------------------------------------------------------------
# vr.pairing.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_pairing_list_returns_flat_pairings_and_replication_servers() -> None:
    vrs_path = f"{_PAIRINGS}/{_PAIRING}/vrs"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        pairings = mock.get(_PAIRINGS).respond(200, json=_PAIRING_PAYLOAD)
        mock.get(vrs_path).respond(200, json=_VRS_PAYLOAD)
        local = mock.get(f"{vrs_path}/vrms-local-0001/replication-servers").respond(
            200, json=_SERVERS_PAYLOAD
        )
        remote = mock.get(f"{vrs_path}/vrms-remote-0001/replication-servers").respond(
            403, json={"error_code": "made-up", "message": "remote site not logged in"}
        )
        result = await _dispatch("vr.pairing.list", {})

    assert result.status == "ok", result.error
    assert pairings.calls[0].request.headers["x-dr-session"] == "vr-session-1"
    assert pairings.calls[0].request.url.params["limit"] == str(PAGE_SIZE)
    assert local.call_count == 1 and remote.call_count == 1
    body = result.result
    assert body["count"] == 1 and body["more_rows"] is False
    row = body["items"][0]
    assert row["pairing_id"] == _PAIRING
    assert row["local_vc_id"] == "vc-local-0001"
    assert row["remote_vrms_name"] == "<remote-vr>"
    assert row["local_vrms_status"] == "OK"
    assert row["local_vc_build"] == "22222222"
    assert all(not isinstance(value, (dict, list)) for value in row.values())
    assert body["replication_servers"] == [
        {
            "pairing_id": _PAIRING,
            "vrms_id": "vrms-local-0001",
            "vrms_name": "<local-vr>",
            "id": "vrs-0001",
            "name": "<replication-server>",
            "hostname": "<replication-server-host>",
            "uuid": "vrs-uuid-0001",
            "connected": True,
            "decommissioned": False,
            "internally_managed": True,
            "replication_count": 3,
            "fault": None,
        }
    ]
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "403"
    assert "remote site not logged in" in body["errors"][0]["message"]
    assert "made-up-pem" not in json.dumps(body)


# ---------------------------------------------------------------------------
# vr.replication.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_replication_list_asks_for_extended_info_and_returns_rpo_fields() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications"
    payload = {
        "list": [_replication(1), _replication(2, rpo_violation=True)],
        "_meta": {"total": 2, "errors": []},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        route = mock.get(path).respond(200, json=payload)
        result = await _dispatch(
            "vr.replication.list", {"pairing_id": _PAIRING, "source_vc_guid": "vc-local-0001"}
        )

    assert result.status == "ok", result.error
    query = route.calls[0].request.url.params
    assert query["extended_info"] == "true"
    assert query["source_vc_guid"] == "vc-local-0001"
    assert query["offset"] == "0"
    rows = result.result["items"]
    assert [row["rpo_met"] for row in rows] == [True, False]
    second = rows[1]
    assert second["status"] == "RPO_VIOLATION"
    assert second["rpo_violation"] is True
    assert second["rpo_minutes"] == 15
    assert second["current_rpo_violation"] == 30
    assert second["last_sync_time"] == 1700000000000
    assert second["sync_progress"] == 100
    assert second["last_group_error"] == "made-up error"
    assert second["last_group_error_details"] == "made-up details"
    assert second["pairing_id"] == _PAIRING
    assert "disks" not in result.result
    assert all(not isinstance(value, (dict, list)) for value in second.values())


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_replication_list_reads_every_page() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications"
    total = PAGE_SIZE + 1

    def _page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        rows = [_replication(i) for i in range(offset, min(offset + PAGE_SIZE, total))]
        return httpx.Response(200, json={"list": rows, "_meta": {"total": total}})

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        route = mock.get(path).mock(side_effect=_page)
        result = await _dispatch("vr.replication.list", {"pairing_id": _PAIRING})

    assert result.status == "ok", result.error
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", str(PAGE_SIZE)]
    assert result.result["count"] == total
    assert result.result["more_rows"] is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_replication_list_with_name_and_disks_reads_the_disks() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications"
    disks = {
        "list": [
            {
                "vm_disk": {
                    "device_key": 2000,
                    "label": "Hard disk 1",
                    "is_vm_home": False,
                    "capacity": 1048576,
                    "source_path": {"datastore_name": "<source-datastore>", "path": "<vm-1>"},
                },
                "replicated": True,
                "destination_path": {"datastore_name": "<target-datastore>", "path": "<vm-1>"},
                "destination_disk_format": "THIN",
                "replica_disk_usage": 524288,
            }
        ],
        "_meta": {"total": 1},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        route = mock.get(path).respond(200, json={"list": [_replication(1)], "_meta": {}})
        disk_route = mock.get(f"{path}/replication-0001/disks").respond(200, json=disks)
        result = await _dispatch(
            "vr.replication.list",
            {"pairing_id": _PAIRING, "name": "<vm-1>", "include_disks": True},
        )

    assert result.status == "ok", result.error
    query = route.calls[0].request.url.params
    assert query["filter_property"] == "name"
    assert query["filter"] == "<vm-1>"
    assert disk_route.call_count == 1
    disk = result.result["disks"][0]
    assert disk["replication_id"] == "replication-0001"
    assert disk["label"] == "Hard disk 1"
    assert disk["capacity_kb"] == 1048576
    assert disk["source_datastore_name"] == "<source-datastore>"
    assert disk["destination_datastore_name"] == "<target-datastore>"
    assert disk["destination_disk_format"] == "THIN"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_include_disks_on_too_many_replications_stops_before_any_disk_read() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications"
    rows = [_replication(i) for i in range(MAX_DISK_READS + 1)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(path).respond(200, json={"list": rows, "_meta": {"total": len(rows)}})
        disk_route = mock.get(url__regex=r".*/disks$").respond(200, json={"list": []})
        result = await _dispatch(
            "vr.replication.list", {"pairing_id": _PAIRING, "include_disks": True}
        )

    assert result.status == "error"
    assert disk_route.call_count == 0
    rendered = (result.error or "") + json.dumps(result.extras, default=str)
    assert "include_disks" in rendered and "name" in rendered


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_a_dot_dot_id_sends_no_http_request() -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        anything = mock.get(url__regex=r".*/pairings/.*").respond(200, json={"list": []})
        result = await _dispatch("vr.replication.list", {"pairing_id": ".."})

    assert result.status == "error"
    assert anything.call_count == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_401_on_a_list_read_logs_in_again_and_the_read_succeeds() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications"

    def _answer(request: httpx.Request) -> httpx.Response:
        if request.headers["x-dr-session"] == "vr-session-1":
            return httpx.Response(401)
        return httpx.Response(200, json={"list": [_replication(1)], "_meta": {"total": 1}})

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("vr-session-1"), _login("vr-session-2")]
        mock.get(path).mock(side_effect=_answer)
        result = await _dispatch("vr.replication.list", {"pairing_id": _PAIRING})

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert result.result["count"] == 1


# ---------------------------------------------------------------------------
# vr.replication.issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_replication_issues_returns_flat_rows_and_partial_data_reasons() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications/issues"
    payload = {
        "list": [
            {
                "entity": "replication-0002",
                "entity_name": "<vm-2>",
                "description": "RPO of 15 minutes is not met",
                "timestamp": 1700000000000,
                "status": "ERROR",
                "issue_type": "VRMS",
            }
        ],
        "_meta": {
            "total": 1,
            "errors": [{"code": "made-up-code", "message": "remote site", "field": "list"}],
        },
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        route = mock.get(path).respond(200, json=payload)
        result = await _dispatch(
            "vr.replication.issues", {"pairing_id": _PAIRING, "source_vc_guid": "vc-local-0001"}
        )

    assert result.status == "ok", result.error
    assert route.calls[0].request.url.params["source_vc_guid"] == "vc-local-0001"
    assert result.result["items"] == [payload["list"][0]]
    assert result.result["errors"] == [
        {"code": "made-up-code", "message": "remote site", "field": "list"}
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_vendor_error_reaches_the_answer() -> None:
    path = f"{_PAIRINGS}/{_PAIRING}/replications/issues"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(path).respond(404, json={"error_code": "made-up", "message": "no pairing"})
        result = await _dispatch("vr.replication.issues", {"pairing_id": _PAIRING})

    assert result.status == "error"
    assert "404" in (result.error or "") + json.dumps(result.extras, default=str)


# ---------------------------------------------------------------------------
# vr.vm.check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_vm_check_reads_capability_and_target_datastores_of_the_target_vcenter() -> None:
    capability = f"{_PAIRINGS}/{_PAIRING}/vcenters/vc-local-0001/vms/vm-7/capability"
    datastores = f"{_PAIRINGS}/{_PAIRING}/vcenters/vc-remote-0001/datastores"
    capability_payload = {
        "vm_id": "vm-7",
        "vm_validation_status": "IS_VM_TEMPLATE",
        "replication_not_supported_reasons": ["The VM is a template"],
        "min_rpo_mins": 5,
        "quiescing_supported": True,
        "mpit_supported": True,
        "_meta": {"errors": []},
    }
    datastore_payload = {
        "list": [
            {
                "id": "datastore-11",
                "name": "<target-datastore>",
                "type": "DATASTORE",
                "file_system_type": "VSAN",
                "capacity": 1000,
                "free_space": 600,
                "overall_status": "GREEN",
                "accessible": True,
            }
        ],
        "_meta": {"total": 1},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(capability).respond(200, json=capability_payload)
        ds_route = mock.get(datastores).respond(200, json=datastore_payload)
        result = await _dispatch(
            "vr.vm.check",
            {
                "pairing_id": _PAIRING,
                "vcenter_id": "vc-local-0001",
                "vm_id": "vm-7",
                "target_vcenter_id": "vc-remote-0001",
            },
        )

    assert result.status == "ok", result.error
    assert ds_route.call_count == 1
    body = result.result
    assert body["vm_validation_status"] == "IS_VM_TEMPLATE"
    assert body["replication_possible"] is False
    assert body["replication_not_supported_reasons"] == ["The VM is a template"]
    assert body["min_rpo_mins"] == 5
    assert body["capability"]["mpit_supported"] is True
    assert body["target_vcenter_id"] == "vc-remote-0001"
    assert body["items"][0]["name"] == "<target-datastore>"
    assert body["items"][0]["free_space"] == 600


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_vm_check_lists_datastores_of_the_vm_vcenter_by_default() -> None:
    base = f"{_PAIRINGS}/{_PAIRING}/vcenters/vc-local-0001"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(f"{base}/vms/vm-7/capability").respond(
            200, json={"vm_validation_status": "OK", "replication_not_supported_reasons": None}
        )
        ds_route = mock.get(f"{base}/datastores").respond(200, json={"list": []})
        result = await _dispatch(
            "vr.vm.check", {"pairing_id": _PAIRING, "vcenter_id": "vc-local-0001", "vm_id": "vm-7"}
        )

    assert result.status == "ok", result.error
    assert ds_route.call_count == 1
    assert result.result["replication_possible"] is True
    assert result.result["target_vcenter_id"] == "vc-local-0001"


# ---------------------------------------------------------------------------
# vr.task.get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_task_get_returns_the_task_and_hides_secret_names_in_the_result() -> None:
    payload = {
        "id": "task-0001",
        "description": "Configure replication",
        "description_id": "made-up.description",
        "entity": "replication-0001",
        "entity_name": "<vm-1>",
        "status": "ERROR",
        "error": {"message": "made-up failure", "details": "made-up details"},
        "result": {"replication_id": "replication-0001", "password": _PASSWORD},
        "progress": 40,
        "queued_time": 1700000000000,
        "start_time": 1700000001000,
        "complete_time": 1700000002000,
        "_meta": {},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(f"{_API}/tasks/task-0001").respond(200, json=payload)
        result = await _dispatch("vr.task.get", {"task_id": "task-0001"})

    assert result.status == "ok", result.error
    body = result.result
    assert body["status"] == "ERROR"
    assert body["error"] == "made-up failure"
    assert body["error_details"] == "made-up details"
    assert body["progress"] == 40
    assert body["result"] == {"replication_id": "replication-0001", "password": REDACTED}
    rendered = json.dumps(body) + json.dumps(result.extras, default=str)
    assert _PASSWORD not in rendered
    assert "vr-session-1" not in rendered


# ---------------------------------------------------------------------------
# Registered metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_connector")
async def test_new_ops_are_safe_grouped_and_documented(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_NEW_OPS))
            )
        )
        .scalars()
        .all()
    )
    assert {row.op_id for row in rows} == _NEW_OPS
    group_by_op = {op.op_id: op.group_key for op in VR_TYPED_OPS}
    for row in rows:
        assert row.source_kind == "typed"
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert row.group_id is not None
        assert row.op_id in VR_TYPED_WHEN_TO_USE_BY_GROUP[group_by_op[row.op_id]]
        assert row.parameter_schema["additionalProperties"] is False
        assert len(row.parameter_schema["examples"]) == 1
        assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
        assert "\n" not in row.summary and len(row.description) <= 300
