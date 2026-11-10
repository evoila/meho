# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure VM metric reads: one VM, and the utilization summary.

The dispatch tests run the real dispatcher (policy, audit, parameter check),
the login cache and the real result reduction with an in-memory result store.
Microsoft's login service, Azure Resource Manager and the regional metrics host
are mocked with respx; the answers have the shape Microsoft documents. The
clock is fixed. Every id, name and secret here is made up.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    monitor,
    monitor_calls,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.monitor_rows import (
    HourlyMetrics,
    cpu_block,
    memory_block,
    parse_metrics,
    percentile_95,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    SUBSCRIPTION_A,
    SUBSCRIPTION_B,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)
from tests._result_store_helpers import MemoryResultStore

GROUP = "rg-example"
REGION = "westeurope"
NOW = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
END = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
COMPUTE_URL = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Compute"
VMS_URL = f"{COMPUTE_URL}/virtualMachines"
SKUS_URL = f"{COMPUTE_URL}/skus"
BATCH_PATH = f"/subscriptions/{SUBSCRIPTION_A}/metrics:getBatch"
ARM_TOKEN = login_token(n=1)
METRICS_TOKEN = login_token(n=2)
METRICS_OP_IDS = ("azure.vm.metric.list", "azure.vm.utilization.summary")


def batch_url(region: str = REGION) -> str:
    return f"https://{region}.metrics.monitor.azure.com{BATCH_PATH}"


def vm_id(name: str, group: str = GROUP, subscription: str = SUBSCRIPTION_A) -> str:
    return (
        f"/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.Compute/virtualMachines/{name}"
    )


def metrics_url(name: str) -> str:
    return f"{ARM_BASE}{vm_id(name)}/providers/Microsoft.Insights/metrics"


# ---------------------------------------------------------------------------
# Azure answers (made-up names and ids)
# ---------------------------------------------------------------------------


def vm_answer(
    name: str, *, location: str = REGION, size: str | None = "Standard_D2s_v5"
) -> dict[str, Any]:
    properties: dict[str, Any] = {"hardwareProfile": {"vmSize": size}, "storageProfile": {}}
    return {"id": vm_id(name), "name": name, "location": location, "properties": properties}


def status_answer(name: str, power: str = "running") -> dict[str, Any]:
    statuses = [{"code": f"PowerState/{power}", "level": "Info"}]
    return {
        "id": vm_id(name),
        "name": name,
        "location": REGION,
        "properties": {"instanceView": {"statuses": statuses}},
    }


def size_answer(name: str, memory: str) -> dict[str, Any]:
    return {
        "resourceType": "virtualMachines",
        "name": name,
        "locations": [REGION],
        "capabilities": [
            {"name": "vCPUs", "value": "2"},
            {"name": "MemoryGB", "value": memory},
        ],
        "restrictions": [],
    }


def points(
    start: datetime, hours: Iterable[int], *, base: float, peak: float | None = None
) -> list[dict[str, Any]]:
    """Hourly points for the given hour numbers. A missing hour has no point at all."""
    return [
        {
            "timeStamp": (start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "average": base + h,
            "minimum": base,
            "maximum": peak if peak is not None else base + h + 5,
        }
        for h in hours
    ]


def metric(name: str, data: list[dict[str, Any]], *, error: str | None = None) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "id": f"id-{name}",
        "type": "Microsoft.Insights/metrics",
        "name": {"value": name, "localizedValue": name},
        "unit": "Percent",
        "timeseries": [{"data": data}] if data else [],
    }
    if error is not None:
        answer["errorCode"] = error
    return answer


def cpu_and_memory(
    start: datetime,
    cpu_hours: Iterable[int],
    memory_hours: Iterable[int],
    *,
    free_bytes: float = 4 * 2**30,
) -> list[dict[str, Any]]:
    memory_data = [
        {**p, "average": free_bytes, "minimum": free_bytes / 2, "maximum": free_bytes}
        for p in points(start, memory_hours, base=0.0)
    ]
    return [
        metric("Percentage CPU", points(start, cpu_hours, base=10.0)),
        metric("Available Memory Bytes", memory_data),
    ]


def batch_item(name: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resourceid": vm_id(name), "resourceregion": REGION, "value": metrics}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _azure_only_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_calls, "utcnow", lambda: NOW)


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
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def connector(_stub_embedding: AsyncMock, session: AsyncSession) -> AzureConnector:
    del session
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


def _login(request: httpx.Request) -> httpx.Response:
    """Answer the token request with a token that depends on the scope asked for."""
    scope = parse_qs(request.content.decode())["scope"][0]
    token = METRICS_TOKEN if "metrics.monitor" in scope else ARM_TOKEN
    return httpx.Response(200, json=token_answer(token))


def _mock_login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).mock(side_effect=_login)


def _mock_vms(
    mock: respx.MockRouter, vms: list[dict[str, Any]], states: list[dict[str, Any]] | None = None
) -> None:
    status = states if states is not None else [status_answer(v["name"]) for v in vms]

    def answer(request: httpx.Request) -> httpx.Response:
        body = status if request.url.params.get("statusOnly") == "true" else vms
        return httpx.Response(200, json={"value": body})

    mock.get(VMS_URL).mock(side_effect=answer)


def _mock_sizes(mock: respx.MockRouter, memory: str = "8") -> respx.Route:
    return mock.get(SKUS_URL).respond(200, json={"value": [size_answer("Standard_D2s_v5", memory)]})


async def _call(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params,
    )


async def _query(result: Any, spec: dict[str, Any]) -> dict[str, Any]:
    return await result_query_module.run_result_query(
        operator(), result.handle.handle_id, ResultQuerySpec.model_validate(spec)
    )


# ---------------------------------------------------------------------------
# azure.vm.metric.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metric_list_gives_every_hour_and_keeps_gaps_as_null(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    start = END - timedelta(days=7)
    present = [h for h in range(168) if h not in (10, 11, 12)]
    answer = {"value": cpu_and_memory(start, present, present)}
    with respx.mock() as mock:
        login = _mock_login(mock)
        mock.get(f"{ARM_BASE}{vm_id('vm-a')}").respond(200, json=vm_answer("vm-a"))
        _mock_sizes(mock, "8")
        arm = mock.get(metrics_url("vm-a")).respond(200, json=answer)
        result = await _call("azure.vm.metric.list", {"vm_id": vm_id("vm-a")})

    assert result.status == "ok", result.error
    assert login.call_count == 1  # one token, for Resource Manager only
    query = arm.calls[0].request.url.params
    assert query["api-version"] == "2023-10-01"
    assert query["metricnames"] == "Percentage CPU,Available Memory Bytes"
    assert query["interval"] == "PT1H"
    assert query["aggregation"] == "Average,Minimum,Maximum"
    assert query["timespan"] == "2026-10-13T12:00:00Z/2026-10-20T12:00:00Z"
    assert arm.calls[0].request.headers["authorization"] == f"Bearer {ARM_TOKEN}"
    summary = result.result
    assert summary["samples_expected"] == 168
    assert summary["samples_found"] == 165
    assert summary["coverage_percent"] == 98.2
    assert summary["memory_gb"] == 8.0
    assert summary["window_from"] == "2026-10-13T12:00:00Z"
    assert summary["window_to"] == "2026-10-20T12:00:00Z"
    assert summary["row_count"] == 168  # more than 50 rows: a stored result
    rows = (await _query(result, {"limit": 200}))["rows"]
    by_time = {row["time"]: row for row in rows}
    gap = by_time["2026-10-13T22:00:00Z"]
    assert gap["cpu_average_percent"] is None and gap["memory_used_percent_average"] is None
    first = by_time["2026-10-13T12:00:00Z"]
    assert first["cpu_average_percent"] == 10.0
    assert first["memory_used_percent_average"] == 50.0  # 4 of 8 GB free
    assert first["memory_used_percent_peak"] == 75.0  # 2 of 8 GB free at the lowest
    assert first["memory_available_bytes_average"] == 4 * 2**30
    assert first["memory_available_bytes_minimum"] == 2 * 2**30
    assert first["memory_available_bytes_maximum"] == 4 * 2**30
    assert gap["memory_available_bytes_maximum"] is None


@pytest.mark.asyncio
async def test_metric_list_takes_a_given_window(connector: AzureConnector) -> None:
    del connector
    with respx.mock() as mock:
        _mock_login(mock)
        mock.get(f"{ARM_BASE}{vm_id('vm-a')}").respond(200, json=vm_answer("vm-a"))
        _mock_sizes(mock)
        arm = mock.get(metrics_url("vm-a")).respond(200, json={"value": []})
        result = await _call(
            "azure.vm.metric.list",
            {"vm_id": vm_id("vm-a"), "from": "2026-10-01T00:30:00Z", "to": "2026-10-01T04:45:00Z"},
        )

    assert result.status == "ok", result.error
    assert (
        arm.calls[0].request.url.params["timespan"] == "2026-10-01T00:00:00Z/2026-10-01T04:00:00Z"
    )
    assert [row["time"] for row in result.result["value"]] == [
        f"2026-10-01T0{h}:00:00Z" for h in range(4)
    ]
    # An answer with no metrics at all means no samples: unknown, not zero.
    assert result.result["samples_found"] == 0
    assert all(row["cpu_average_percent"] is None for row in result.result["value"])


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-06-01T00:00:00Z"},  # longer than 93 days
        {"to": "2026-10-21T00:00:00Z"},  # in the future
        {"from": "2026-10-20T12:10:00Z", "to": "2026-10-20T12:50:00Z"},  # less than an hour
        {"from": "yesterday"},
    ],
)
@pytest.mark.asyncio
async def test_metric_list_refuses_a_bad_window(
    connector: AzureConnector, params: dict[str, Any]
) -> None:
    del connector
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={"value": []})
        result = await _call("azure.vm.metric.list", {"vm_id": vm_id("vm-a"), **params})

    assert result.status == "error"
    assert not arm.called


@pytest.mark.parametrize(
    "bad_id",
    [
        vm_id("vm-a", subscription=SUBSCRIPTION_B),  # another subscription
        vm_id("vm-a", group="..") + "/x",
        vm_id("vm-a/../../other"),
        f"{vm_id('vm-a')}?x=1",
        "/subscriptions/x/resourceGroups/g/providers/Microsoft.Network/virtualNetworks/n",
    ],
)
@pytest.mark.asyncio
async def test_metric_list_refuses_an_id_that_is_not_a_vm_of_the_target(
    connector: AzureConnector, bad_id: str
) -> None:
    del connector
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={"value": []})
        result = await _call("azure.vm.metric.list", {"vm_id": bad_id})

    assert result.status == "error"
    assert not arm.called


@pytest.mark.asyncio
async def test_metric_list_answers_not_found_for_an_unknown_vm(connector: AzureConnector) -> None:
    del connector
    error = {"error": {"code": "ResourceNotFound", "message": "The resource was not found."}}
    with respx.mock() as mock:
        _mock_login(mock)
        mock.get(f"{ARM_BASE}{vm_id('vm-gone')}").respond(404, json=error)
        result = await _call("azure.vm.metric.list", {"vm_id": vm_id("vm-gone")})

    assert result.status == "not_found"


@pytest.mark.asyncio
async def test_metric_list_fails_when_azure_fails_it_does_not_return_empty_rows(
    connector: AzureConnector,
) -> None:
    del connector
    with respx.mock() as mock:
        _mock_login(mock)
        mock.get(f"{ARM_BASE}{vm_id('vm-a')}").respond(200, json=vm_answer("vm-a"))
        _mock_sizes(mock)
        mock.get(metrics_url("vm-a")).respond(
            403, json={"error": {"code": "AuthorizationFailed", "message": "No access."}}
        )
        result = await _call("azure.vm.metric.list", {"vm_id": vm_id("vm-a")})

    assert result.status == "error"
    assert result.extras["exception_class"] == "AzureForbiddenError"


@pytest.mark.asyncio
async def test_metric_list_counts_free_memory_samples_when_the_size_is_unknown(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    start = END - timedelta(days=7)
    with respx.mock() as mock:
        _mock_login(mock)
        mock.get(f"{ARM_BASE}{vm_id('vm-a')}").respond(200, json=vm_answer("vm-a"))
        mock.get(SKUS_URL).respond(500, json={"error": {"code": "InternalError"}})
        mock.get(metrics_url("vm-a")).respond(
            200, json={"value": cpu_and_memory(start, range(168), range(168))}
        )
        result = await _call("azure.vm.metric.list", {"vm_id": vm_id("vm-a")})

    assert result.status == "ok", result.error
    assert result.result["memory_gb"] is None
    assert result.result["memory_samples_found"] == 168  # every hour has a free-memory sample
    rows = (await _query(result, {"limit": 200}))["rows"]
    assert len(rows) == 168
    assert all(row["memory_available_bytes_maximum"] == 4 * 2**30 for row in rows)
    assert all(row["memory_used_percent_average"] is None for row in rows)


@pytest.mark.asyncio
async def test_metric_list_says_when_memory_size_is_unknown_and_when_a_metric_failed(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    start = END - timedelta(days=7)
    cpu = metric("Percentage CPU", points(start, range(168), base=10.0))
    memory = metric("Available Memory Bytes", [], error="BadRequest")
    with respx.mock() as mock:
        _mock_login(mock)
        mock.get(f"{ARM_BASE}{vm_id('vm-a')}").respond(200, json=vm_answer("vm-a"))
        mock.get(SKUS_URL).respond(500, json={"error": {"code": "InternalError"}})
        mock.get(metrics_url("vm-a")).respond(200, json={"value": [cpu, memory]})
        result = await _call("azure.vm.metric.list", {"vm_id": vm_id("vm-a")})

    assert result.status == "ok", result.error
    assert result.result["memory_gb"] is None
    assert result.result["memory_samples_found"] == 0
    note = result.result["note"]
    assert "could not be read" in note and "Azure reported an error for the memory metric" in note
    rows = (await _query(result, {"limit": 5}))["rows"]
    assert all(row["memory_used_percent_average"] is None for row in rows)
    assert all(row["cpu_average_percent"] is not None for row in rows)


# ---------------------------------------------------------------------------
# azure.vm.utilization.summary
# ---------------------------------------------------------------------------


def _batch_answers(
    calls: list[tuple[str, list[str]]], per_vm: dict[str, list[dict[str, Any]]]
) -> Any:
    """Answer a batch call for the VMs it names, and record the call."""

    def answer(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["resourceids"]
        calls.append((request.url.host, ids))
        items = [
            {"resourceid": rid, "value": per_vm[rid.rsplit("/", 1)[-1]]}
            for rid in ids
            if rid.rsplit("/", 1)[-1] in per_vm
        ]
        return httpx.Response(200, json={"values": items})

    return answer


@pytest.mark.asyncio
async def test_summary_of_120_vms_uses_three_batch_calls_and_the_metrics_token(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    names = [f"vm-{n:03d}" for n in range(120)]
    start = END - timedelta(days=1)
    per_vm = {name: cpu_and_memory(start, range(24), range(24)) for name in names}
    calls: list[tuple[str, list[str]]] = []
    with respx.mock() as mock:
        login = _mock_login(mock)
        _mock_vms(mock, [vm_answer(n) for n in names])
        sizes = _mock_sizes(mock, "8")
        batch = mock.post(batch_url()).mock(side_effect=_batch_answers(calls, per_vm))
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    assert result.status == "ok", result.error
    assert batch.call_count == 3
    assert [len(ids) for _host, ids in calls] == [50, 50, 20]
    assert {host for host, _ids in calls} == {f"{REGION}.metrics.monitor.azure.com"}
    assert sizes.call_count == 1  # one size list for the one region
    assert login.call_count == 2  # one token for Resource Manager, one for the metrics host
    for call in batch.calls:
        query = call.request.url.params
        assert call.request.headers["authorization"] == f"Bearer {METRICS_TOKEN}"
        assert query["api-version"] == "2024-02-01"
        assert query["metricnamespace"] == "Microsoft.Compute/virtualMachines"
        assert query["metricnames"] == "Percentage CPU,Available Memory Bytes"
        assert query["interval"] == "PT1H"
        assert query["starttime"] == "2026-10-19T12:00:00Z"
        assert query["endtime"] == "2026-10-20T12:00:00Z"
    summary = result.result
    assert summary["row_count"] == 120  # a stored result
    assert summary["vms_total"] == 120 and summary["vms_sufficient"] == 120
    assert summary["batches"] == 3 and summary["batches_failed"] == 0
    assert summary["window_days"] == 1 and summary["samples_expected"] == 24
    assert summary["window_from"] == "2026-10-19T12:00:00Z"
    assert "never counted as zero" in summary["method"] and len(summary["method"]) < 500
    rows = (await _query(result, {"limit": 200}))["rows"]
    row = next(r for r in rows if r["name"] == "vm-007")
    assert row["cpu"] == {
        "average_percent": 21.5,  # 10 + mean of 0..23
        "p95_percent": 32.0,  # nearest rank: the 23rd of 24 values
        "maximum_percent": 38.0,  # highest hourly maximum, 10 + 23 + 5
        "samples_found": 24,
        "coverage_percent": 100.0,
    }
    assert row["memory"]["memory_gb"] == 8.0
    assert row["memory"]["average_percent"] == 50.0
    assert row["memory"]["maximum_percent"] == 75.0
    assert row["samples_expected"] == 24 and row["samples_found"] == 24
    assert row["sufficient"] is True and row["reason"] is None


@pytest.mark.asyncio
async def test_summary_marks_a_stopped_vm_with_missing_hours_and_never_counts_zero(
    connector: AzureConnector,
) -> None:
    del connector
    start = END - timedelta(days=1)
    per_vm = {
        "vm-on": cpu_and_memory(start, range(24), range(24)),
        # Deallocated for 14 of 24 hours: Azure returns no point for them.
        "vm-off": cpu_and_memory(start, range(10), range(10)),
        # Just enough: 20 of 24 hours is 83.3%.
        "vm-edge": cpu_and_memory(start, range(20), range(20)),
        # Below 80%: 19 of 24 hours is 79.2%.
        "vm-low": cpu_and_memory(start, range(19), range(19)),
    }
    vms = [vm_answer(n) for n in per_vm]
    states = [status_answer("vm-off", "deallocated")] + [
        status_answer(n) for n in ("vm-on", "vm-edge", "vm-low")
    ]
    calls: list[tuple[str, list[str]]] = []
    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, vms, states)
        _mock_sizes(mock)
        mock.post(batch_url()).mock(side_effect=_batch_answers(calls, per_vm))
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    assert result.status == "ok", result.error
    rows = {row["name"]: row for row in result.result["value"]}
    off = rows["vm-off"]
    assert off["samples_found"] == 10 and off["samples_expected"] == 24
    assert off["coverage_percent"] == 41.7
    assert off["sufficient"] is False
    assert "Only 10 of 24" in off["reason"] and "off now" in off["reason"]
    # The average is over the 10 hours that have a sample (10..19), not over 24 with zeros.
    assert off["cpu"]["average_percent"] == 14.5
    assert rows["vm-edge"]["sufficient"] is True and rows["vm-edge"]["coverage_percent"] == 83.3
    assert rows["vm-low"]["sufficient"] is False and rows["vm-low"]["coverage_percent"] == 79.2
    assert result.result["vms_sufficient"] == 2


@pytest.mark.asyncio
async def test_summary_marks_a_vm_that_azure_gave_no_data_for(connector: AzureConnector) -> None:
    del connector
    start = END - timedelta(days=1)
    per_vm = {"vm-a": cpu_and_memory(start, range(24), range(24))}
    calls: list[tuple[str, list[str]]] = []
    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, [vm_answer("vm-a"), vm_answer("vm-b")])
        _mock_sizes(mock)
        mock.post(batch_url()).mock(side_effect=_batch_answers(calls, per_vm))
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    rows = {row["name"]: row for row in result.result["value"]}
    unknown = rows["vm-b"]
    assert unknown["reason"] == "Azure returned no metric data for this VM."
    assert unknown["sufficient"] is False and unknown["samples_found"] == 0
    assert unknown["cpu"]["average_percent"] is None and unknown["cpu"]["p95_percent"] is None
    assert unknown["memory"]["average_percent"] is None


@pytest.mark.asyncio
async def test_summary_reports_one_failed_batch_and_still_counts_the_others(
    connector: AzureConnector, store: MemoryResultStore
) -> None:
    del connector, store
    names = [f"vm-{n:03d}" for n in range(60)]  # a batch of 50 and a batch of 10
    start = END - timedelta(days=1)
    per_vm = {name: cpu_and_memory(start, range(24), range(24)) for name in names}
    calls: list[tuple[str, list[str]]] = []
    good = _batch_answers(calls, per_vm)

    def answer(request: httpx.Request) -> httpx.Response:
        if len(json.loads(request.content)["resourceids"]) == 10:
            return httpx.Response(400, json={"error": {"code": "BadRequest", "message": "no"}})
        return good(request)

    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, [vm_answer(n) for n in names])
        _mock_sizes(mock)
        mock.post(batch_url()).mock(side_effect=answer)
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    assert result.status == "ok", result.error
    assert result.result["batches"] == 2 and result.result["batches_failed"] == 1
    rows = (await _query(result, {"limit": 100}))["rows"]
    failed = [r for r in rows if not r["sufficient"]]
    assert len(failed) == 10
    assert all("HTTP 400" in r["reason"] and "unknown, not idle" in r["reason"] for r in failed)
    assert all(r["cpu"]["average_percent"] is None for r in failed)
    assert result.result["vms_sufficient"] == 50


@pytest.mark.asyncio
async def test_summary_fails_when_the_login_may_not_read_metrics(
    connector: AzureConnector,
) -> None:
    del connector
    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, [vm_answer("vm-a")])
        _mock_sizes(mock)
        mock.post(batch_url()).respond(
            403, json={"error": {"code": "AuthorizationFailed", "message": "No access."}}
        )
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    assert result.status == "error"
    assert result.extras["exception_class"] == "AzureForbiddenError"


@pytest.mark.asyncio
async def test_summary_groups_by_region_and_sends_the_token_only_to_those_regions(
    connector: AzureConnector,
) -> None:
    del connector
    start = END - timedelta(days=1)
    per_vm = {n: cpu_and_memory(start, range(24), range(24)) for n in ("vm-a", "vm-b", "vm-c")}
    vms = [
        vm_answer("vm-a"),
        vm_answer("vm-b", location="Northeurope"),
        vm_answer("vm-c", location="evil.example/x"),
    ]
    calls: list[tuple[str, list[str]]] = []
    with respx.mock(assert_all_called=False) as mock:
        _mock_login(mock)
        _mock_vms(mock, vms)
        mock.get(SKUS_URL).respond(200, json={"value": []})
        west = mock.post(batch_url("westeurope")).mock(side_effect=_batch_answers(calls, per_vm))
        north = mock.post(batch_url("northeurope")).mock(side_effect=_batch_answers(calls, per_vm))
        other = mock.post(url__regex=r"https://(?!westeurope|northeurope).*").respond(200, json={})
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    assert result.status == "ok", result.error
    assert west.call_count == 1 and north.call_count == 1 and not other.called
    rows = {row["name"]: row for row in result.result["value"]}
    assert "no usable region" in rows["vm-c"]["reason"]
    assert rows["vm-c"]["sufficient"] is False
    # The size list was empty, so memory use is unknown and says why.
    assert rows["vm-a"]["memory"]["average_percent"] is None
    assert "memory size" in rows["vm-a"]["memory"]["reason"]
    assert rows["vm-a"]["sufficient"] is True  # sufficiency follows the CPU samples


@pytest.mark.asyncio
async def test_summary_takes_vm_ids_and_names_a_vm_that_does_not_exist(
    connector: AzureConnector,
) -> None:
    del connector
    start = END - timedelta(days=1)
    per_vm = {"vm-a": cpu_and_memory(start, range(24), range(24))}
    calls: list[tuple[str, list[str]]] = []
    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, [vm_answer("vm-a"), vm_answer("vm-other")])
        _mock_sizes(mock)
        mock.post(batch_url()).mock(side_effect=_batch_answers(calls, per_vm))
        result = await _call(
            "azure.vm.utilization.summary",
            {"days": 1, "vm_ids": [vm_id("vm-a"), vm_id("vm-gone"), vm_id("VM-A")]},
        )

    assert result.status == "ok", result.error
    assert [ids for _host, ids in calls] == [[vm_id("vm-a")]]
    rows = {row["name"]: row for row in result.result["value"]}
    assert set(rows) == {"vm-a", "vm-gone"}  # the repeated ID counts once
    assert rows["vm-gone"]["reason"] == "No VM with this ID exists in the subscription."
    assert rows["vm-gone"]["sufficient"] is False


@pytest.mark.asyncio
async def test_summary_of_a_resource_group_lists_only_that_group(connector: AzureConnector) -> None:
    del connector
    group_url = (
        f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}"
        "/providers/Microsoft.Compute/virtualMachines"
    )
    with respx.mock() as mock:
        _mock_login(mock)
        listed = mock.get(group_url).respond(200, json={"value": []})
        result = await _call("azure.vm.utilization.summary", {"resource_group": GROUP})

    assert result.status == "ok", result.error
    assert listed.call_count == 1
    assert result.result["value"] == [] and result.result["batches"] == 0
    assert result.result["window_days"] == 30 and result.result["samples_expected"] == 720


@pytest.mark.parametrize(
    "params",
    [
        {"vm_ids": [vm_id("vm-a")], "resource_group": GROUP},
        {"vm_ids": [vm_id("vm-a", subscription=SUBSCRIPTION_B)]},
        {"vm_ids": ["not-an-id"]},
        {"days": 0},
        {"days": 94},
        {"days": "30"},
        {"resource_group": "../providers"},
    ],
)
@pytest.mark.asyncio
async def test_summary_refuses_bad_parameters(
    connector: AzureConnector, params: dict[str, Any]
) -> None:
    del connector
    with respx.mock(assert_all_called=False) as mock:
        _mock_login(mock)
        anything = mock.route().respond(200, json={"value": []})
        result = await _call("azure.vm.utilization.summary", params)

    assert result.status == "error"
    assert not [c for c in anything.calls if "Compute" in str(c.request.url)]


@pytest.mark.asyncio
async def test_summary_reads_the_most_vms_it_allows_and_says_what_it_left_out(
    connector: AzureConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    del connector
    monkeypatch.setattr("meho_backplane.connectors.azure.monitor_summary.MAX_VMS", 3)
    start = END - timedelta(days=1)
    names = [f"vm-{n}" for n in range(5)]
    per_vm = {n: cpu_and_memory(start, range(24), range(24)) for n in names}
    calls: list[tuple[str, list[str]]] = []
    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, [vm_answer(n) for n in names])
        _mock_sizes(mock)
        mock.post(batch_url()).mock(side_effect=_batch_answers(calls, per_vm))
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    assert result.status == "ok", result.error
    assert result.result["truncated"] is True and result.result["omission_kind"] == "vm_limit"
    assert "first 3 VMs" in result.result["omission"]
    assert [row["name"] for row in result.result["value"]] == ["vm-0", "vm-1", "vm-2"]


@pytest.mark.asyncio
async def test_summary_refuses_a_cloud_other_than_the_public_one() -> None:
    target = AzureTargetStub(
        host="management.usgovcloudapi.net",
        extras={"subscription_id": SUBSCRIPTION_A, "cloud": "AzureUSGovernment"},
    )
    instance = AzureConnector(credentials_loader=CountingLoader())
    with pytest.raises(ValueError, match="AzureCloud cloud only"):
        await instance.vm_utilization_summary(operator(), target, {})


@pytest.mark.asyncio
async def test_no_token_or_secret_reaches_a_result(connector: AzureConnector) -> None:
    del connector
    start = END - timedelta(days=1)
    per_vm = {"vm-a": cpu_and_memory(start, range(24), range(24))}
    calls: list[tuple[str, list[str]]] = []
    with respx.mock() as mock:
        _mock_login(mock)
        _mock_vms(mock, [vm_answer("vm-a")])
        _mock_sizes(mock)
        mock.post(batch_url()).mock(side_effect=_batch_answers(calls, per_vm))
        result = await _call("azure.vm.utilization.summary", {"days": 1})

    text = json.dumps(result.model_dump(mode="json"))
    assert ARM_TOKEN not in text and METRICS_TOKEN not in text
    assert "fake-client-secret" not in text


# ---------------------------------------------------------------------------
# Reading the answers
# ---------------------------------------------------------------------------

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def test_a_gap_is_left_out_not_counted_as_zero() -> None:
    parsed = parse_metrics(
        [metric("Percentage CPU", points(START, [0, 1, 3], base=50.0))], START, 4
    )
    assert sorted(parsed.cpu) == [0, 1, 3]
    block = cpu_block(parsed.cpu, 4)
    assert block.samples_found == 3 and block.coverage_percent == 75.0
    assert block.average == round((50 + 51 + 53) / 3, 2)  # not divided by 4
    assert block.sufficient is False


def test_odd_points_are_ignored() -> None:
    data = [
        {"timeStamp": "2026-10-01T00:00:00Z", "average": 10.0, "maximum": 11.0},
        {"timeStamp": "2026-10-01T00:00:00Z", "average": 99.0},  # the same hour again
        {"timeStamp": "2026-10-01T01:00:00Z", "average": float("nan")},
        {"timeStamp": "2026-10-01T02:00:00Z", "average": True},
        {"timeStamp": "2026-10-01T03:00:00Z", "average": "12"},
        {"timeStamp": "2026-10-01T03:30:00Z", "average": 5.0},  # not on the hour
        {"timeStamp": "2026-09-30T23:00:00Z", "average": 5.0},  # before the window
        {"timeStamp": "2026-10-01T04:00:00Z", "average": 5.0},  # after the window
        {"timeStamp": "bad", "average": 5.0},
        {"average": 5.0},
        "text",
    ]
    parsed = parse_metrics([metric("Percentage CPU", data), "text", {"name": 3}], START, 4)
    assert parsed.cpu == {0: {"average": 10.0, "maximum": 11.0}}


def test_a_metric_with_an_error_code_has_no_points_and_is_named() -> None:
    parsed = parse_metrics(
        [
            metric("Percentage CPU", points(START, [0], base=1.0), error="Success"),
            metric("Available Memory Bytes", points(START, [0], base=1.0), error="Failed"),
            metric("Network In", points(START, [0], base=1.0)),
        ],
        START,
        2,
    )
    assert list(parsed.cpu) == [0] and parsed.memory == {} and parsed.failed == ["memory"]


@pytest.mark.parametrize("answer", [None, {}, [], "x", [None], [{"timeseries": "x"}]])
def test_a_missing_or_odd_answer_has_no_points(answer: object) -> None:
    parsed = parse_metrics(answer, START, 4)
    assert parsed == HourlyMetrics()


def test_percentile_is_nearest_rank() -> None:
    assert percentile_95([]) is None
    assert percentile_95([7.0]) == 7.0
    assert percentile_95([float(n) for n in range(1, 21)]) == 19.0  # 20 values: the 19th
    assert percentile_95([float(n) for n in range(1, 101)]) == 95.0


def test_memory_used_is_kept_between_0_and_100_and_unknown_without_a_size() -> None:
    free = {0: {"average": 1 * 2**30, "minimum": 9 * 2**30}, 1: {"average": 40 * 2**30}}
    block = memory_block(free, 8.0, 2)
    assert block.average == round(
        (87.5 + 0.0) / 2, 2
    )  # more free than the size says: 0, not negative
    assert block.maximum == 0.0
    none = memory_block(free, None, 2)
    assert (none.average, none.p95, none.maximum) == (None, None, None)
    assert none.samples_found == 2 and none.coverage_percent == 100.0  # samples still counted
    assert none.sufficient is False  # but there is no number to size from
    assert math.isfinite(block.p95 or 0.0)


@pytest.mark.parametrize(("found", "sufficient"), [(403, False), (404, True)])
def test_sufficiency_compares_counts_not_the_rounded_coverage(found: int, sufficient: bool) -> None:
    expected = 21 * 24  # 504 hours; 403 is 79.96%, which rounds to 80.0
    cpu = {h: {"average": 10.0, "maximum": 20.0} for h in range(found)}
    free = {h: {"average": 4 * 2**30, "minimum": 2 * 2**30} for h in range(found)}
    cpu_stats = cpu_block(cpu, expected)
    memory_stats = memory_block(free, 8.0, expected)
    assert (
        cpu_stats.coverage_percent
        == memory_stats.coverage_percent
        == round(100 * found / expected, 1)
    )
    assert cpu_stats.sufficient is sufficient
    assert memory_stats.sufficient is sufficient


# ---------------------------------------------------------------------------
# Registration and contracts
# ---------------------------------------------------------------------------


def test_the_family_is_in_the_operation_table() -> None:
    by_op = {op.op_id: op for op in AZURE_TYPED_OPS}
    assert [op.op_id for op in monitor.OPS] == list(METRICS_OP_IDS)
    for op_id in METRICS_OP_IDS:
        op = by_op[op_id]
        assert op.group_key == "monitor"
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert write_word_in_op_id(op_id) is None
        assert callable(getattr(AzureConnector, op.handler_attr))
        assert op.llm_instructions is not None
        assert {"when_to_call", "output_shape", "next_step"} <= set(op.llm_instructions)
    assert AZURE_TYPED_WHEN_TO_USE_BY_GROUP["monitor"] == monitor.WHEN_TO_USE["monitor"]


@pytest.mark.asyncio
async def test_the_monitor_operations_are_stored_as_typed_safe_reads(
    connector: AzureConnector, session: AsyncSession
) -> None:
    del connector
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == AZURE_PRODUCT,
                    EndpointDescriptor.op_id.in_(METRICS_OP_IDS),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.op_id for row in rows} == set(METRICS_OP_IDS)
    for row in rows:
        assert row.source_kind == "typed"
        assert row.safety_level == "safe" and row.requires_approval is False
    group = (
        await session.execute(
            select(OperationGroup).where(
                OperationGroup.product == AZURE_PRODUCT, OperationGroup.group_key == "monitor"
            )
        )
    ).scalar_one()
    assert group.when_to_use == monitor.WHEN_TO_USE["monitor"]
