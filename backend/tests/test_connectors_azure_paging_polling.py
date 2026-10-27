# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Azure paging (``nextLink`` and ``$skipToken``) and for following
long-running operations, plus ``azure.operation.status`` and the throttling
error through the real dispatcher.

Azure Resource Manager and Microsoft's login service are mocked with respx.
No test waits for real. Every id, name and secret here is made up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_VERSION,
    AzureConnector,
    _http,
    _paging,
    _polling,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure._errors import AzureUrlRefusedError
from meho_backplane.connectors.azure._paging import (
    PageLimits,
    list_all_pages,
    query_resource_graph,
)
from meho_backplane.connectors.azure._polling import (
    OperationReference,
    operation_reference,
    read_operation_state,
    wait_for_operation,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import Settings, get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_HOST,
    SUBSCRIPTION_A,
    SUBSCRIPTION_B,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

RESOURCES_PATH = f"/subscriptions/{SUBSCRIPTION_A}/resources"
RESOURCES_URL = f"{ARM_BASE}{RESOURCES_PATH}"
GRAPH_URL = f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources"
OPERATION_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Compute/locations/"
    "westeurope/operations/22222222-0000-0000-0000-00000000f001?api-version=2024-07-01"
)
RESULT_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Storage/operations/"
    "22222222-0000-0000-0000-00000000f002?monitor=true&api-version=2023-05-01"
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    fake = _Clock()
    for module in (_http, _paging, _polling):
        monkeypatch.setattr(module, "_monotonic", fake.monotonic)
    for module in (_http, _polling):
        monkeypatch.setattr(module, "_sleep", fake.sleep)
    yield fake


def _connector() -> AzureConnector:
    return AzureConnector(credentials_loader=CountingLoader())


def _page(names: list[str], next_link: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"value": [{"name": name} for name in names]}
    if next_link is not None:
        body["nextLink"] = next_link
    return body


def _next(n: int, subscription: str = SUBSCRIPTION_A, host: str = "management.azure.com") -> str:
    return (
        f"https://{host}/subscriptions/{subscription}/resources"
        f"?api-version=2021-04-01&$skiptoken=page{n}"
    )


async def _list(limits: PageLimits | None = None) -> Any:
    return await list_all_pages(
        _connector(),
        AzureTargetStub(),
        RESOURCES_PATH,
        operator=operator(),
        api_version="2021-04-01",
        params={"$top": 5},
        limits=limits,
    )


# ----------------------------------------------------------------------
# Paging
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_link_is_followed_over_three_pages(clock: _Clock) -> None:
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(url__startswith=RESOURCES_URL).mock(
            side_effect=[
                httpx.Response(200, json=_page(["a", "b"], _next(2))),
                httpx.Response(200, json=_page(["c", "d"], _next(3))),
                httpx.Response(200, json=_page(["e"])),
            ]
        )
        paged = await _list()

    assert [row["name"] for row in paged.rows] == ["a", "b", "c", "d", "e"]
    assert paged.pages == 3
    assert paged.truncated is False
    assert paged.omissions == []
    first, second, third = (call.request.url for call in arm.calls)
    assert first.params["api-version"] == "2021-04-01" and first.params["$top"] == "5"
    assert second.params["$skiptoken"] == "page2"
    assert third.params["$skiptoken"] == "page3"
    assert second.params.get_list("api-version") == ["2021-04-01"]


@pytest.mark.asyncio
async def test_the_page_limit_says_what_it_left_out(clock: _Clock) -> None:
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(url__startswith=RESOURCES_URL).mock(
            side_effect=[httpx.Response(200, json=_page([f"r{n}"], _next(n + 1))) for n in range(5)]
        )
        paged = await _list(PageLimits(max_pages=2))

    assert arm.call_count == 2
    assert paged.truncated is True
    (omission,) = paged.omissions
    assert omission["kind"] == "page_limit"
    assert omission["limit"] == 2
    assert omission["pages_read"] == 2
    assert omission["rows_returned"] == 2
    assert "Narrow the request" in omission["message"]
    assert paged.as_dict()["truncated"] is True


@pytest.mark.asyncio
async def test_the_row_limit_cuts_and_says_so(clock: _Clock) -> None:
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(url__startswith=RESOURCES_URL).mock(
            side_effect=[
                httpx.Response(200, json=_page(["a", "b", "c"], _next(2))),
                httpx.Response(200, json=_page(["d", "e", "f"], _next(3))),
            ]
        )
        paged = await _list(PageLimits(max_rows=4))

    assert [row["name"] for row in paged.rows] == ["a", "b", "c", "d"]
    assert paged.truncated is True
    assert paged.omissions[0]["kind"] == "row_limit"


def test_the_default_row_limit_is_the_stored_result_cap() -> None:
    assert Settings.model_fields["result_handle_max_spill_rows"].default == _paging.MAX_ROWS
    assert (_paging.MAX_PAGES, _paging.MAX_SECONDS) == (50, 120.0)


@pytest.mark.asyncio
async def test_the_time_limit_stops_paging(clock: _Clock) -> None:
    def slow_page(request: httpx.Request) -> httpx.Response:
        clock.now += 70
        return httpx.Response(200, json=_page(["x"], _next(9)))

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(url__startswith=RESOURCES_URL).mock(side_effect=slow_page)
        paged = await _list()

    assert arm.call_count == 2
    assert paged.omissions[0]["kind"] == "time_limit"


@pytest.mark.parametrize(
    "bad_link",
    [_next(2, host="attacker.example.net"), _next(2, subscription=SUBSCRIPTION_B)],
)
@pytest.mark.asyncio
async def test_a_next_link_outside_the_target_is_refused_before_a_token_is_sent(
    clock: _Clock, bad_link: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(url__startswith=RESOURCES_URL).respond(200, json=_page(["a"], bad_link))
        other = mock.get(url__startswith="https://attacker.example.net").respond(200)
        elsewhere = mock.get(url__startswith=f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_B}").respond(
            200
        )
        with pytest.raises(AzureUrlRefusedError):
            await _list()

    assert not other.called
    assert not elsewhere.called


@pytest.mark.asyncio
async def test_resource_graph_follows_the_skip_token(clock: _Clock) -> None:
    query = {"query": "Resources | project id", "subscriptions": [SUBSCRIPTION_A]}
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        graph = mock.post(url__startswith=GRAPH_URL).mock(
            side_effect=[
                httpx.Response(200, json={"data": [{"id": "1"}], "$skipToken": "next-1"}),
                httpx.Response(200, json={"data": [{"id": "2"}]}),
            ]
        )
        paged = await query_resource_graph(
            _connector(), AzureTargetStub(), query, operator=operator()
        )

    assert [row["id"] for row in paged.rows] == ["1", "2"]
    first, second = (json.loads(call.request.content) for call in graph.calls)
    assert "options" not in first
    assert second["options"] == {"$skipToken": "next-1"}
    assert second["query"] == query["query"]
    assert graph.calls[0].request.url.params["api-version"] == "2024-04-01"


@pytest.mark.asyncio
async def test_a_resource_graph_query_is_tried_again_after_a_5xx(clock: _Clock) -> None:
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        graph = mock.post(url__startswith=GRAPH_URL).mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"data": []})]
        )
        await query_resource_graph(
            _connector(), AzureTargetStub(), {"query": "Resources"}, operator=operator()
        )

    assert graph.call_count == 2


# ----------------------------------------------------------------------
# Long-running operations
# ----------------------------------------------------------------------


def _accepted(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(202, headers=headers, request=httpx.Request("PUT", ARM_BASE))


def test_the_async_operation_header_wins_over_location() -> None:
    reference = operation_reference(
        AzureTargetStub(),
        _accepted({"Azure-AsyncOperation": OPERATION_URL, "Location": RESULT_URL}),
        resource_id="/subscriptions/x/resourceGroups/rg",
    )
    assert reference is not None
    assert reference.poll_kind == "azure-asyncoperation"
    assert reference.poll_url == OPERATION_URL
    assert reference.resource_id == "/subscriptions/x/resourceGroups/rg"
    assert reference.started_at


def test_a_finished_answer_has_no_reference() -> None:
    done = httpx.Response(200, json={}, request=httpx.Request("PUT", ARM_BASE))
    assert operation_reference(AzureTargetStub(), done, resource_id=None) is None


@pytest.mark.parametrize(
    "url",
    [
        OPERATION_URL.replace("management.azure.com", "attacker.example.net"),
        OPERATION_URL.replace(SUBSCRIPTION_A, SUBSCRIPTION_B),
        f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg?api-version=2021-04-01",
        f"{OPERATION_URL}&pad={'a' * 4100}",
    ],
)
def test_a_bad_poll_url_is_refused(url: str) -> None:
    with pytest.raises(AzureUrlRefusedError):
        operation_reference(AzureTargetStub(), _accepted({"Location": url}), resource_id=None)


@pytest.mark.asyncio
async def test_an_async_operation_is_polled_with_retry_after_until_it_succeeds(
    clock: _Clock,
) -> None:
    reference = OperationReference(OPERATION_URL, "azure-asyncoperation", None, "t0")
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        poll = mock.get(OPERATION_URL).mock(
            side_effect=[
                httpx.Response(200, json={"status": "InProgress"}, headers={"Retry-After": "3"}),
                httpx.Response(200, json={"status": "Running"}),
                httpx.Response(200, json={"status": "Succeeded", "properties": {"x": 1}}),
            ]
        )
        outcome = await wait_for_operation(
            _connector(), AzureTargetStub(), reference, operator=operator()
        )

    assert poll.call_count == 3
    assert clock.sleeps == [3.0, _polling.DEFAULT_POLL_SECONDS]
    assert outcome["status"] == "Succeeded" and outcome["done"] is True
    assert outcome["result"] == {"x": 1}
    assert outcome["operation"]["poll_url"] == OPERATION_URL


@pytest.mark.asyncio
async def test_a_failed_async_operation_returns_azures_error(clock: _Clock) -> None:
    reference = OperationReference(OPERATION_URL, "azure-asyncoperation", None, "t0")
    failed = {"status": "Failed", "error": {"code": "QuotaExceeded", "message": "No quota."}}
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(OPERATION_URL).respond(200, json=failed)
        state = await read_operation_state(
            _connector(), AzureTargetStub(), reference, operator=operator()
        )

    assert (state.status, state.done) == ("Failed", True)
    assert state.error == {"code": "QuotaExceeded", "message": "No quota."}


@pytest.mark.asyncio
async def test_a_location_poll_answers_202_until_done_then_the_result(clock: _Clock) -> None:
    reference = OperationReference(RESULT_URL, "location", None, "t0")
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(RESULT_URL).mock(
            side_effect=[
                httpx.Response(202, headers={"Retry-After": "17"}),
                httpx.Response(200, json={"name": "exampleaccount"}),
            ]
        )
        outcome = await wait_for_operation(
            _connector(), AzureTargetStub(), reference, operator=operator()
        )

    assert clock.sleeps == [17.0]
    assert outcome["status"] == "Succeeded"
    assert outcome["result"] == {"name": "exampleaccount"}


@pytest.mark.asyncio
async def test_a_location_poll_error_means_the_operation_failed(clock: _Clock) -> None:
    reference = OperationReference(RESULT_URL, "location", None, "t0")
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(RESULT_URL).respond(
            400, json={"error": {"code": "InvalidTemplate", "message": "Bad input."}}
        )
        state = await read_operation_state(
            _connector(), AzureTargetStub(), reference, operator=operator()
        )

    assert (state.status, state.done) == ("Failed", True)
    assert state.error == {"code": "InvalidTemplate", "message": "Bad input."}


@pytest.mark.asyncio
async def test_waiting_stops_when_the_time_is_up_and_returns_the_reference(
    clock: _Clock,
) -> None:
    reference = OperationReference(OPERATION_URL, "azure-asyncoperation", None, "t0")
    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(OPERATION_URL).respond(
            200, json={"status": "InProgress"}, headers={"Retry-After": "20"}
        )
        outcome = await wait_for_operation(
            _connector(), AzureTargetStub(), reference, operator=operator(), max_seconds=50
        )

    assert outcome["done"] is False
    assert clock.sleeps == [20.0, 20.0]
    assert outcome["operation"] == reference.as_dict()


# ----------------------------------------------------------------------
# Through the dispatcher
# ----------------------------------------------------------------------


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


async def _fresh_connector() -> None:
    """Register the ops and give the dispatcher's (new) instance a stub loader."""
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()


@pytest.mark.asyncio
async def test_operation_status_reads_the_outcome_after_a_restart(
    _dispatch_env: None, clock: _Clock
) -> None:
    reference = OperationReference(OPERATION_URL, "azure-asyncoperation", None, "t0").as_dict()
    await _fresh_connector()
    # A restart: every cached connector, secret and token is gone.
    reset_handler_cache()
    await _fresh_connector()

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        poll = mock.get(OPERATION_URL).respond(200, json={"status": "Succeeded"})
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.operation.status",
            target=AzureTargetStub(),
            params={"operation": reference},
        )
        sent = [call.request.method for call in mock.calls if call.request.url.host == ARM_HOST]

    assert result.status == "ok", result.error
    assert result.result["status"] == "Succeeded"
    assert result.result["done"] is True
    assert poll.call_count == 1
    assert sent == ["GET"]


@pytest.mark.asyncio
async def test_operation_status_refuses_a_reference_of_another_subscription(
    _dispatch_env: None, clock: _Clock
) -> None:
    await _fresh_connector()
    reference = OperationReference(
        OPERATION_URL.replace(SUBSCRIPTION_A, SUBSCRIPTION_B), "azure-asyncoperation", None, "t0"
    ).as_dict()

    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.operation.status",
            target=AzureTargetStub(),
            params={"operation": reference},
        )

    assert result.status == "error"
    assert result.extras["exception_class"] == "AzureUrlRefusedError"
    assert not login.called


@pytest.mark.asyncio
async def test_the_dispatcher_adds_no_tries_and_returns_the_throttling_error(
    _dispatch_env: None, clock: _Clock
) -> None:
    await _fresh_connector()
    throttled = httpx.Response(
        429,
        headers={"Retry-After": "5"},
        json={"error": {"code": "TooManyRequests", "message": "Slow down."}},
    )

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(url__startswith=f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}").mock(
            side_effect=[httpx.Response(401)] + [throttled] * 10
        )
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.about",
            target=AzureTargetStub(),
            params={},
        )

    assert arm.call_count == _http.MAX_ATTEMPTS
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "AzureThrottledError"
    assert result.extras["http_status"] == 429
    assert "TooManyRequests" in result.extras["upstream_message"]
    assert "in 5 seconds" in result.extras["remediation"]
    assert login_token() not in json.dumps(result.model_dump(mode="json"))
