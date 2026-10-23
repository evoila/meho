# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX reads, with a respx-mocked HCX Manager and no ingest.

* each read sends the right method, path, query and body, with the
  ``x-hm-authorization`` token, and returns the documented fields;
* list reads come back as ``{"items": [...]}`` with one flat row per vendor
  object, and a long list goes behind a result handle;
* a 401 makes a new login and the read then succeeds, through ``dispatch()``;
* a vendor error (404) reaches the answer;
* no password, token, private key or licence key comes back in clear text,
  and none is stored in the flight-recorder trace of the call;
* the diagnostics flow: start a run, list the runs, read the findings;
* the inventory reads for a migration: compute tree, datastores, networks;
* every op is registered typed, ``safe``, without approval, in a group.

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
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.hcx.read_ops import HCX_READ_OPS
from meho_backplane.connectors.hcx.typed_ops import HCX_TYPED_OPS, HCX_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.hcx.typed_reads import (
    REDACTED,
    flatten_inventory_tree,
    manager_role,
    redact_secrets,
    rows_of,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, EndpointDescriptor, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_HOST = "hcx-reads.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSIONS = "/hybridity/api/sessions"
_TOKEN = "reads-hm-token-1"
_PASSWORD = "reads-password-value"
_MESH_ID = "mesh-0001"
_JOB_ID = "job-0001"
_DIAG_ID = "diag-0001"
_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000d4")


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


class _HcxTarget:
    """Target satisfying both ``HcxTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = HCX_PRODUCT
        self.fingerprint = type("_FP", (), {"version": HCX_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "hcx-reads"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/hcx-reads"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-reads-hcx-typed",
        name="HCX Reads Operator",
        email=None,
        raw_jwt="op.reads.hcx.typed.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-reads-svc", "password": _PASSWORD}


async def _register(_stub_embedding: AsyncMock) -> None:
    await register_hcx_typed_operations()
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]


async def _dispatch(op_id: str, params: dict[str, Any] | None = None) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id=op_id,
        target=_HcxTarget(),
        params=params or {},
    )


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_SESSIONS).respond(200, headers={"x-hm-authorization": _TOKEN})


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content or b"null")


# ---------------------------------------------------------------------------
# hcx.about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_returns_version_role_and_local_site(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        appliances = mock.post("/hybridity/api/hcspAppliances").respond(
            200,
            json=[{"systemName": "<manager>", "systemType": "HCX Cloud", "version": "4.11.0.0"}],
        )
        clouds = mock.post("/hybridity/api/service/inventory/cloud/list").respond(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "endpointId": "ep-local",
                            "name": "site-a",
                            "url": "https://manager.test.invalid",
                            "endpointType": "VC",
                        }
                    ]
                },
            },
        )
        result = await _dispatch("hcx.about")

    assert result.status == "ok", result.error
    assert appliances.calls[0].request.headers["x-hm-authorization"] == _TOKEN
    assert _body(appliances) == {"action": "query", "filter": {"local": "true"}}
    assert _body(clouds) == {"filter": {"local": True, "remote": False}}
    assert result.result == {
        "version": "4.11.0.0",
        "build": None,
        "system_type": "HCX Cloud",
        "system_name": "<manager>",
        "role": "cloud",
        "local_endpoint_id": "ep-local",
        "local_endpoint_name": "site-a",
        "local_endpoint_type": "VC",
        "local_url": "https://manager.test.invalid",
    }


@pytest.mark.parametrize(
    ("system_type", "role"),
    [
        ("HCX Cloud", "cloud"),
        ("HCX Connector", "connector"),
        ("HCX-Manager", "unknown"),
        (None, "unknown"),
    ],
)
def test_manager_role(system_type: Any, role: str) -> None:
    assert manager_role(system_type) == role


def test_rows_of_reads_every_envelope() -> None:
    row = {"id": "a"}
    assert rows_of([row, "x"]) == [row]
    assert rows_of({"items": [row]}) == [row]
    assert rows_of({"data": {"items": [row]}}) == [row]
    assert rows_of({"data": None}) == []
    assert rows_of("text") == []


# ---------------------------------------------------------------------------
# Site pairings, service meshes, appliances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_site_pairing_list_returns_rows_without_the_password(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/hybridity/api/cloudConfigs").respond(
            200,
            json={
                "success": True,
                "data": {
                    "items": [
                        {
                            "endpointId": "ep-remote",
                            "cloudType": "<cloud-type>",
                            "url": "https://remote.test.invalid",
                            "username": "<user>",
                            "password": _PASSWORD,
                        }
                    ]
                },
            },
        )
        result = await _dispatch("hcx.site_pairing.list")

    assert result.status == "ok", result.error
    (row,) = result.result["items"]
    assert row["endpointId"] == "ep-remote"
    assert row["password"] == REDACTED
    assert _PASSWORD not in json.dumps(result.result)


async def _enable_trace_capture(session: AsyncSession) -> None:
    """Seed the operator's tenant with the flight recorder on."""
    session.add(
        Tenant(id=_TENANT_ID, slug="hcx-reads", name="hcx-reads", flight_recorder_enabled=True)
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _trace_spans(audit_id: UUID) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        return list(
            (
                await s.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == trace.id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_site_pairing_password_is_masked_in_the_recorded_trace(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The stored trace of the vendor call holds the masked body, never the password."""
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/hybridity/api/cloudConfigs").respond(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "endpointId": "ep-remote",
                            "username": "<user>",
                            "password": _PASSWORD,
                            "remote": {"vcPassword": _PASSWORD, "url": "https://r.test.invalid"},
                        }
                    ]
                }
            },
        )
        result = await _dispatch("hcx.site_pairing.list")

    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _trace_spans(result.audit_id)
    vendor_calls = [span for span in spans if span.span_kind == "vendor_call"]
    assert len(vendor_calls) == 1
    span = vendor_calls[0]
    assert span.name == f"GET {_BASE_URL}/hybridity/api/cloudConfigs"
    assert span.attributes["body_recorded"] is True
    (row,) = span.attributes["response_body"]["data"]["items"]
    assert row["endpointId"] == "ep-remote"
    assert row["password"] == REDACTED
    assert row["remote"]["vcPassword"] == REDACTED
    rendered = json.dumps([f"{s.name}|{s.attributes}" for s in spans], default=str)
    assert _PASSWORD not in rendered
    assert _TOKEN not in rendered


def test_redact_secrets_masks_names_that_end_with_a_secret_word() -> None:
    row = {
        "vcPassword": "a",
        "admin_password": "b",
        "sessionToken": "c",
        "sslPrivateKey": "d",
        "tokenExpiry": 5,
        "name": "site-a",
    }
    assert redact_secrets(row) == {
        "vcPassword": REDACTED,
        "admin_password": REDACTED,
        "sessionToken": REDACTED,
        "sslPrivateKey": REDACTED,
        "tokenExpiry": 5,
        "name": "site-a",
    }


@pytest.mark.asyncio
async def test_service_mesh_list_adds_the_last_task_fields(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/hybridity/api/interconnect/serviceMesh").respond(
            200,
            json={
                "items": [
                    {
                        "serviceMeshId": _MESH_ID,
                        "name": "mesh-a",
                        "state": "<state>",
                        "lastTaskDetails": {
                            "interconnectTaskId": "task-1",
                            "type": "<task-type>",
                            "status": "<task-status>",
                        },
                    },
                    {"serviceMeshId": "mesh-0002", "name": "mesh-b"},
                ]
            },
        )
        result = await _dispatch("hcx.service_mesh.list")

    assert result.status == "ok", result.error
    first, second = result.result["items"]
    assert first["serviceMeshId"] == _MESH_ID
    assert first["last_task_id"] == "task-1"
    assert first["last_task_type"] == "<task-type>"
    assert first["last_task_status"] == "<task-status>"
    assert second["last_task_id"] is None


@pytest.mark.asyncio
async def test_appliance_list_adds_state_tunnel_and_version_fields(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/hybridity/api/interconnect/appliances").respond(
            200,
            json={
                "items": [
                    {
                        "applianceId": "app-1",
                        "applianceType": "HCX-NET-EXT",
                        "serviceMeshId": _MESH_ID,
                        "applianceStatus": {"state": "<state>"},
                        "applianceVersion": {"major": 4, "minor": 11, "patch": 0, "build": 42},
                        "updateInfo": {"updateAvailable": False},
                        "status": {
                            "summary": {
                                "overallStatus": "<overall>",
                                "overallTransportStatus": "<transport>",
                                "overallEncryptionTunnelStatus": "<tunnel>",
                                "encryptionTunnelSummary": {
                                    "totalActiveTunnelCount": 1,
                                    "totalTunnelCount": 2,
                                },
                            }
                        },
                        "config": {"privateKey": "made-up-key-material"},
                    }
                ]
            },
        )
        result = await _dispatch("hcx.appliance.list")

    assert result.status == "ok", result.error
    (row,) = result.result["items"]
    assert row["state"] == "<state>"
    assert row["overall_status"] == "<overall>"
    assert row["transport_status"] == "<transport>"
    assert row["tunnel_status"] == "<tunnel>"
    assert (row["tunnels_active"], row["tunnels_total"]) == (1, 2)
    assert (row["version"], row["build"]) == ("4.11.0", 42)
    assert row["update_available"] is False
    assert row["config"]["privateKey"] == REDACTED


# ---------------------------------------------------------------------------
# Diagnostics, jobs, alerts, inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_mesh_diagnose_posts_to_the_mesh_and_returns_the_task(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post(f"/hybridity/api/interconnect/serviceMesh/{_MESH_ID}/diagnose").respond(
            202,
            json={"data": {"interconnectTaskId": "task-9", "serviceMeshId": _MESH_ID}},
        )
        result = await _dispatch("hcx.service_mesh.diagnose", {"service_mesh_id": _MESH_ID})

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert result.result == {
        "service_mesh_id": _MESH_ID,
        "interconnect_task_id": "task-9",
        "warnings": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_service_mesh_diagnose_needs_the_mesh_id(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = _login(mock)
        result = await _dispatch("hcx.service_mesh.diagnose", {})

    assert result.status == "error"
    assert login.call_count == 0


@pytest.mark.asyncio
async def test_job_get_reads_one_job(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await _register(_stub_embedding)
    job = {"jobId": _JOB_ID, "isDone": True, "didFail": False, "percentComplete": 100}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.get(f"/hybridity/api/jobs/{_JOB_ID}").respond(200, json=job)
        result = await _dispatch("hcx.job.get", {"job_id": _JOB_ID})

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert result.result == job


@pytest.mark.asyncio
async def test_job_get_vendor_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get("/hybridity/api/jobs/job-unknown").respond(
            404, json={"errors": [{"code": "<code>", "message": "Job not found"}]}
        )
        result = await _dispatch("hcx.job.get", {"job_id": "job-unknown"})

    assert result.status == "error"
    assert result.extras["http_status"] == 404
    assert "Job not found" in result.extras["upstream_message"]


_PATH_IDS = [
    ("a/b", "a%2Fb"),
    ("a?action=query", "a%3Faction%3Dquery"),
    ("a#b", "a%23b"),
    ("a/../../x", None),
    ("..", None),
    (".", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw_id", "encoded"), _PATH_IDS)
async def test_service_mesh_diagnose_keeps_the_mesh_id_one_path_segment(
    _stub_embedding: AsyncMock, session: AsyncSession, raw_id: str, encoded: str | None
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        other = mock.route().respond(202, json={"data": {}})
        result = await _dispatch("hcx.service_mesh.diagnose", {"service_mesh_id": raw_id})

    if encoded is None:
        assert result.status == "error"
        assert other.call_count == 0
        return
    assert result.status == "ok", result.error
    assert other.call_count == 1
    request = other.calls[0].request
    assert request.method == "POST"
    assert request.url.raw_path == (
        f"/hybridity/api/interconnect/serviceMesh/{encoded}/diagnose".encode()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw_id", "encoded"), _PATH_IDS)
async def test_job_get_keeps_the_job_id_one_path_segment(
    _stub_embedding: AsyncMock, session: AsyncSession, raw_id: str, encoded: str | None
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        other = mock.route().respond(200, json={"jobId": "x"})
        result = await _dispatch("hcx.job.get", {"job_id": raw_id})

    if encoded is None:
        assert result.status == "error"
        assert other.call_count == 0
        return
    assert result.status == "ok", result.error
    assert other.call_count == 1
    request = other.calls[0].request
    assert request.method == "GET"
    assert request.url.raw_path == f"/hybridity/api/jobs/{encoded}".encode()


@pytest.mark.asyncio
async def test_alert_list_sends_the_query_action_and_the_filter(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    alert = {"alarm_id": "alert-1", "severity": "<severity>", "message": "<text>"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post("/hybridity/api/alerts").respond(200, json={"items": [alert]})
        filtered = await _dispatch(
            "hcx.alert.list",
            {"severity": ["<severity>"], "state": "<state>", "entity_id": "obj-1"},
        )
        unfiltered = await _dispatch("hcx.alert.list")

    assert filtered.status == "ok", filtered.error
    assert unfiltered.status == "ok", unfiltered.error
    assert route.calls[0].request.url.params["action"] == "query"
    assert _body(route, 0) == {
        "filter": {"severity": ["<severity>"], "state": "<state>", "entityId": "obj-1"}
    }
    assert _body(route, 1) == {}
    assert filtered.result["items"] == [alert]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "body"),
    [
        ({}, {}),
        ({"site": "local"}, {"filter": {"cloud": {"local": True, "remote": False}}}),
        ({"site": "remote"}, {"filter": {"cloud": {"local": False, "remote": True}}}),
    ],
)
async def test_inventory_list_maps_the_site_filter(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    params: dict[str, Any],
    body: dict[str, Any],
) -> None:
    await _register(_stub_embedding)
    row = {"resourceId": "vc-1", "resourceName": "vc-a", "endpointId": "ep-local", "isLocal": True}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post("/hybridity/api/service/inventory/resourcecontainer/list").respond(
            200, json={"success": True, "data": {"items": [row]}}
        )
        result = await _dispatch("hcx.inventory.list", params)

    assert result.status == "ok", result.error
    assert _body(route) == body
    assert result.result["items"] == [row]


@pytest.mark.asyncio
async def test_inventory_list_refuses_an_unknown_site(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = _login(mock)
        result = await _dispatch("hcx.inventory.list", {"site": "everywhere"})

    assert result.status == "error"
    assert login.call_count == 0


# ---------------------------------------------------------------------------
# Diagnostics flow: start a run, list the runs, read the findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_flow_from_start_to_findings(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    run = {
        "diagId": _DIAG_ID,
        "serviceMeshId": _MESH_ID,
        "applianceIds": ["appliance-1"],
        "startTime": 1000,
        "endTime": 2000,
        "status": "<done-status>",
        "message": "<summary>",
        "triggeredBy": "<user>",
        "reason": None,
        "details": {},
    }
    findings = {**run, "details": {"appliance-1": {"tunnel": "<finding>"}}}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        start = mock.post(f"/hybridity/api/interconnect/serviceMesh/{_MESH_ID}/diagnose").respond(
            202, json={"data": {"interconnectTaskId": "task-9", "serviceMeshId": _MESH_ID}}
        )
        query = mock.post(
            f"/hybridity/api/interconnect/serviceMesh/{_MESH_ID}/diagnostics/query"
        ).respond(200, json={"items": [run], "paging": {"offset": 0, "pageSize": 10}})
        details = mock.get(
            f"/hybridity/api/interconnect/serviceMesh/{_MESH_ID}/diagnostics/{_DIAG_ID}"
        ).respond(200, json={"data": findings, "warnings": [{"message": "<warning>"}]})

        started = await _dispatch("hcx.service_mesh.diagnose", {"service_mesh_id": _MESH_ID})
        every_run = await _dispatch(
            "hcx.service_mesh.diagnostics.list", {"service_mesh_id": _MESH_ID}
        )
        done_runs = await _dispatch(
            "hcx.service_mesh.diagnostics.list",
            {"service_mesh_id": _MESH_ID, "status": ["<done-status>"]},
        )
        result = await _dispatch(
            "hcx.service_mesh.diagnostics.get",
            {"service_mesh_id": _MESH_ID, "diag_id": _DIAG_ID},
        )

    assert started.status == "ok", started.error
    assert started.result["interconnect_task_id"] == "task-9"
    assert start.call_count == 1
    assert every_run.status == "ok", every_run.error
    assert every_run.result["items"] == [run]
    assert _body(query, 0) == {"filter": {}}
    assert done_runs.status == "ok", done_runs.error
    assert _body(query, 1) == {"filter": {"status": ["<done-status>"]}}
    assert result.status == "ok", result.error
    assert details.call_count == 1
    assert result.result == {**findings, "warnings": [{"message": "<warning>"}], "errors": []}


@pytest.mark.asyncio
async def test_diagnostics_get_keeps_both_ids_one_path_segment(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        other = mock.route().respond(200, json={"data": {}})
        encoded = await _dispatch(
            "hcx.service_mesh.diagnostics.get", {"service_mesh_id": "m/1", "diag_id": "d?x"}
        )
        refused = await _dispatch(
            "hcx.service_mesh.diagnostics.get", {"service_mesh_id": _MESH_ID, "diag_id": "../x"}
        )
        refused_list = await _dispatch(
            "hcx.service_mesh.diagnostics.list", {"service_mesh_id": "../x"}
        )

    assert encoded.status == "ok", encoded.error
    assert encoded.result == {"details": None, "warnings": [], "errors": []}
    assert other.call_count == 1
    assert other.calls[0].request.url.raw_path == (
        b"/hybridity/api/interconnect/serviceMesh/m%2F1/diagnostics/d%3Fx"
    )
    assert refused.status == "error"
    assert refused_list.status == "error"


# ---------------------------------------------------------------------------
# Inventory for a migration: compute tree, datastores, networks
# ---------------------------------------------------------------------------


def test_flatten_inventory_tree_keeps_order_parent_and_depth() -> None:
    cluster = {"entity_id": "cl-1", "name": "cluster-a", "entityType": "ClusterComputeResource"}
    datacenter = {
        "entity_id": "dc-1",
        "name": "dc-a",
        "entityType": "Datacenter",
        "children": [cluster],
    }
    vcenter = {
        "vcenter_instanceId": "vc-1",
        "entity_id": "vc-1",
        "name": "vcenter-a",
        "entityType": "VirtualCenter",
        "children": [datacenter, "not-a-node"],
    }
    rows = flatten_inventory_tree([vcenter, {"entity_id": "vc-2", "children": None}])
    assert [(row["entity_id"], row["parent_id"], row["depth"]) for row in rows] == [
        ("vc-1", None, 0),
        ("dc-1", "vc-1", 1),
        ("cl-1", "dc-1", 2),
        ("vc-2", None, 0),
    ]
    assert all("children" not in row for row in rows)
    assert rows[0]["vcenter_instanceId"] == "vc-1"
    assert "children" in vcenter


@pytest.mark.asyncio
async def test_inventory_compute_list_flattens_the_vendor_tree(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    tree = {
        "vcenter_instanceId": "vc-1",
        "entity_id": "vc-1",
        "name": "vcenter-a",
        "entityType": "VirtualCenter",
        "children": [
            {
                "vcenter_instanceId": "vc-1",
                "entity_id": "dc-1",
                "name": "dc-a",
                "entityType": "Datacenter",
                "children": [
                    {
                        "vcenter_instanceId": "vc-1",
                        "entity_id": "cl-1",
                        "name": "cluster-a",
                        "entityType": "ClusterComputeResource",
                    }
                ],
            }
        ],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post("/hybridity/api/service/inventory/vc/list").respond(
            200, json={"data": {"items": [tree]}}
        )
        result = await _dispatch("hcx.inventory.compute.list")

    assert result.status == "ok", result.error
    assert _body(route) == {}
    rows = result.result["items"]
    assert [(row["name"], row["parent_id"], row["depth"]) for row in rows] == [
        ("vcenter-a", None, 0),
        ("dc-a", "vc-1", 1),
        ("cluster-a", "dc-1", 2),
    ]
    assert all("children" not in row for row in rows)


@pytest.mark.asyncio
async def test_inventory_datastore_list_queries_the_clusters(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    datastore = {"id": "ds-1", "name": "datastore-a", "entity_type": "Datastore"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post("/hybridity/api/service/inventory/vc/datastores/query").respond(
            200, json={"success": True, "data": {"items": [datastore]}}
        )
        result = await _dispatch(
            "hcx.inventory.datastore.list", {"vcenter_id": "vc-1", "cluster_ids": ["cl-1", "cl-2"]}
        )
        missing = await _dispatch("hcx.inventory.datastore.list", {"vcenter_id": "vc-1"})

    assert result.status == "ok", result.error
    assert _body(route) == {
        "filter": {
            "vcenter_instanceId": "vc-1",
            "computeType": "ClusterComputeResource",
            "computeIds": ["cl-1", "cl-2"],
        }
    }
    assert result.result["items"] == [datastore]
    assert missing.status == "error"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_inventory_network_list_filters_by_the_site(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    network = {
        "entity_id": "net-1",
        "name": "network-a",
        "entityType": "DistributedVirtualPortgroup",
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        route = mock.post("/hybridity/api/service/inventory/networks").respond(
            200, json={"data": {"items": [network]}}
        )
        result = await _dispatch("hcx.inventory.network.list", {"endpoint_id": "ep-local"})
        missing = await _dispatch("hcx.inventory.network.list", {})

    assert result.status == "ok", result.error
    assert _body(route) == {"filter": {"cloud": {"endpointId": "ep-local"}}}
    assert result.result["items"] == [network]
    assert missing.status == "error"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Login retry, result handle, registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_makes_a_new_login_and_the_read_then_succeeds(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSIONS)
        login.side_effect = [
            httpx.Response(200, headers={"x-hm-authorization": "reads-hm-token-1"}),
            httpx.Response(200, headers={"x-hm-authorization": "reads-hm-token-2"}),
        ]
        meshes = mock.get("/hybridity/api/interconnect/serviceMesh")
        meshes.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"items": [{"serviceMeshId": _MESH_ID}]}),
        ]
        result = await _dispatch("hcx.service_mesh.list")

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert meshes.calls[0].request.headers["x-hm-authorization"] == "reads-hm-token-1"
    assert meshes.calls[1].request.headers["x-hm-authorization"] == "reads-hm-token-2"
    assert result.result["items"][0]["serviceMeshId"] == _MESH_ID


@pytest.mark.asyncio
async def test_long_alert_list_goes_behind_a_result_handle(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    set_default_reducer(JsonFluxReducer())
    alerts = [{"alarm_id": f"alert-{i}", "severity": "<severity>"} for i in range(120)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.post("/hybridity/api/alerts").respond(200, json={"items": alerts})
        result = await _dispatch("hcx.alert.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 120


@pytest.mark.asyncio
async def test_results_hold_no_password_or_token(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _login(mock)
        mock.get(f"/hybridity/api/jobs/{_JOB_ID}").respond(
            200,
            json={"jobId": _JOB_ID, "token": "made-up-job-token", "licenseKey": "made-up-key"},
        )
        result = await _dispatch("hcx.job.get", {"job_id": _JOB_ID})

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _TOKEN, "made-up-job-token", "made-up-key"):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_reads_are_registered_typed_safe_and_grouped(
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
    first_eight = {
        "hcx.about",
        "hcx.site_pairing.list",
        "hcx.service_mesh.list",
        "hcx.appliance.list",
        "hcx.service_mesh.diagnose",
        "hcx.job.get",
        "hcx.alert.list",
        "hcx.inventory.list",
    }
    expected = first_eight | {
        "hcx.service_mesh.diagnostics.list",
        "hcx.service_mesh.diagnostics.get",
        "hcx.inventory.compute.list",
        "hcx.inventory.datastore.list",
        "hcx.inventory.network.list",
    }
    reads = [op for op in HCX_TYPED_OPS if op.op_id != "hcx.api_file.get"]
    assert {op.op_id for op in HCX_READ_OPS} == first_eight
    assert {op.op_id for op in reads} == expected
    assert expected <= set(rows)
    for op in reads:
        row = rows[op.op_id]
        assert row.source_kind == "typed"
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert row.parameter_schema["examples"]
        assert op.group_key in HCX_TYPED_WHEN_TO_USE_BY_GROUP
        assert "\n" not in row.summary and len(row.description) <= 300
