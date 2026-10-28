# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure cost family: cost query, forecast, budgets, Advisor, prices.

Microsoft's login service, Azure Resource Manager and the public price list
are mocked with respx; the answers follow Microsoft's documented shapes.
The paging helper, the token cache, the URL check and the real result
reduction run as in production. Every id, name and secret here is made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    cost_reads,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure._errors import (
    AzureForbiddenError,
    AzureRequestError,
    AzureThrottledError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure._paging import PageLimits
from meho_backplane.connectors.azure.cost import OPS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import SpillResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
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
    subscription_answer,
    subscription_url,
    token_answer,
    token_url,
)

SCOPE_URL = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}"
QUERY_URL = f"{SCOPE_URL}/providers/Microsoft.CostManagement/query"
FORECAST_URL = f"{SCOPE_URL}/providers/Microsoft.CostManagement/forecast"
BUDGETS_URL = f"{SCOPE_URL}/providers/Microsoft.Consumption/budgets"
ADVISOR_URL = f"{SCOPE_URL}/providers/Microsoft.Advisor/recommendations"
PRICES_URL = "https://prices.azure.com/api/retail/prices"
TODAY = date(2026, 10, 15)
OPS_BY_ID = {op.op_id: op for op in OPS}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_reads, "_today", lambda: TODAY)


def _connector(loader: Any = None) -> AzureConnector:
    return AzureConnector(credentials_loader=loader or CountingLoader())


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


def _cost_answer(
    columns: list[tuple[str, str]], rows: list[list[Any]], next_link: str | None = None
) -> dict[str, Any]:
    """A Cost Management query or forecast answer, as Azure sends it."""
    return {
        "id": f"/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.CostManagement/query/q1",
        "name": "q1",
        "type": "Microsoft.CostManagement/query",
        "properties": {
            "nextLink": next_link,
            "columns": [{"name": name, "type": kind} for name, kind in columns],
            "rows": rows,
        },
    }


_GROUPED_COLUMNS = [
    ("PreTaxCost", "Number"),
    ("ResourceGroup", "String"),
    ("ServiceName", "String"),
    ("Currency", "String"),
]


def _grouped_rows(count: int) -> list[list[Any]]:
    return [[1.25, f"rg-{n}", "Virtual Machines", "EUR"] for n in range(count)]


def _body(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# ----------------------------------------------------------------------
# azure.cost.query
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_query_sends_last_month_as_dates_and_returns_rows_as_objects() -> None:
    with respx.mock() as mock:
        _login(mock)
        query = mock.post(QUERY_URL).respond(
            200,
            json=_cost_answer(
                _GROUPED_COLUMNS,
                [[10.5, "rg-a", "Storage", "EUR"], [4.25, "rg-b", "Virtual Machines", "EUR"]],
            ),
        )
        out = await _connector().cost_query(
            operator(),
            AzureTargetStub(),
            {"period": "last_month", "group_by": ["resource_group", "service"]},
        )

    request = query.calls[0].request
    assert request.url.params["api-version"] == "2026-06-01"
    assert request.headers["Authorization"] == f"Bearer {login_token()}"
    assert _body(query) == {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": "2026-09-01T00:00:00Z", "to": "2026-09-30T23:59:59Z"},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ResourceGroup"},
                {"type": "Dimension", "name": "ServiceName"},
            ],
        },
    }
    assert out["items"] == [
        {"cost": 10.5, "resource_group": "rg-a", "service": "Storage", "currency": "EUR"},
        {"cost": 4.25, "resource_group": "rg-b", "service": "Virtual Machines", "currency": "EUR"},
    ]
    assert out["totals"] == {"cost": 14.75, "currency": "EUR", "currencies": ["EUR"], "rows": 2}
    assert out["period"] == {
        "name": "last_month",
        "from": "2026-09-01",
        "to": "2026-09-30",
        "granularity": "total",
    }
    assert out["scope"] == {"subscription_id": SUBSCRIPTION_A, "resource_group": None}
    assert out["query"] == {
        "cost_type": "actual",
        "group_by": ["resource_group", "service"],
        "cost_column": "PreTaxCost",
    }
    assert out["omissions"] == {
        "truncated": False,
        "kind": None,
        "message": None,
        "pages_read": 1,
    }


@pytest.mark.asyncio
async def test_cost_query_per_day_reads_the_cost_column_and_writes_the_date() -> None:
    """A Microsoft Customer Agreement answers with a Cost column; both names are read."""
    with respx.mock() as mock:
        _login(mock)
        query = mock.post(QUERY_URL).respond(
            200,
            json=_cost_answer(
                [("Cost", "Number"), ("UsageDate", "Number"), ("Currency", "String")],
                [[3.0, 20261001, "EUR"], [2.5, 20261002, "EUR"]],
            ),
        )
        out = await _connector().cost_query(
            operator(), AzureTargetStub(), {"granularity": "daily", "type": "amortized"}
        )

    body = _body(query)
    assert body["type"] == "AmortizedCost"
    assert body["dataset"]["granularity"] == "Daily"
    assert "grouping" not in body["dataset"]
    assert body["timePeriod"] == {"from": "2026-10-01T00:00:00Z", "to": "2026-10-15T23:59:59Z"}
    assert out["items"] == [
        {"cost": 3.0, "date": "2026-10-01", "currency": "EUR"},
        {"cost": 2.5, "date": "2026-10-02", "currency": "EUR"},
    ]
    assert out["period"]["name"] == "month_to_date"
    assert out["query"]["cost_column"] == "Cost"
    assert out["totals"]["cost"] == 5.5


@pytest.mark.asyncio
async def test_cost_query_for_one_resource_group_and_own_days() -> None:
    group_url = f"{SCOPE_URL}/resourceGroups/rg-a/providers/Microsoft.CostManagement/query"
    with respx.mock() as mock:
        _login(mock)
        query = mock.post(group_url).respond(200, json=_cost_answer(_GROUPED_COLUMNS, []))
        out = await _connector().cost_query(
            operator(),
            AzureTargetStub(),
            {"from": "2026-08-01", "to": "2026-08-31", "resource_group": "rg-a"},
        )

    assert query.called
    assert _body(query)["timePeriod"] == {
        "from": "2026-08-01T00:00:00Z",
        "to": "2026-08-31T23:59:59Z",
    }
    assert out["scope"] == {"subscription_id": SUBSCRIPTION_A, "resource_group": "rg-a"}
    assert out["period"]["name"] == "custom"
    assert out["items"] == []
    assert out["totals"] == {"cost": 0.0, "currency": None, "currencies": [], "rows": 0}


@pytest.mark.asyncio
async def test_cost_query_groups_by_a_tag_key() -> None:
    with respx.mock() as mock:
        _login(mock)
        query = mock.post(QUERY_URL).respond(
            200,
            json=_cost_answer(
                [
                    ("PreTaxCost", "Number"),
                    ("TagKey", "String"),
                    ("TagValue", "String"),
                    ("Currency", "String"),
                ],
                [[7.0, "costcenter", "cc-1", "EUR"]],
            ),
        )
        out = await _connector().cost_query(
            operator(), AzureTargetStub(), {"group_by": ["tag:costcenter"]}
        )

    assert _body(query)["dataset"]["grouping"] == [{"type": "TagKey", "name": "costcenter"}]
    assert out["items"] == [
        {"cost": 7.0, "tag_key": "costcenter", "tag_value": "cc-1", "currency": "EUR"}
    ]


@pytest.mark.asyncio
async def test_cost_query_follows_the_next_link_with_the_same_body() -> None:
    next_link = f"{QUERY_URL}?api-version=2026-06-01&$skiptoken=page2"
    with respx.mock() as mock:
        _login(mock)
        query = mock.post(QUERY_URL).mock(
            side_effect=[
                httpx.Response(
                    200, json=_cost_answer(_GROUPED_COLUMNS, _grouped_rows(2), next_link)
                ),
                httpx.Response(200, json=_cost_answer(_GROUPED_COLUMNS, _grouped_rows(1))),
            ]
        )
        out = await _connector().cost_query(
            operator(), AzureTargetStub(), {"group_by": ["resource_group"]}
        )

    assert query.call_count == 2
    assert query.calls[1].request.url.params["$skiptoken"] == "page2"
    assert query.calls[1].request.method == "POST"
    assert _body(query, 1) == _body(query, 0)
    assert len(out["items"]) == 3
    assert out["omissions"]["pages_read"] == 2


@pytest.mark.asyncio
async def test_cost_query_says_what_a_page_limit_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_reads, "PageLimits", lambda: PageLimits(max_pages=1))
    next_link = f"{QUERY_URL}?api-version=2026-06-01&$skiptoken=page2"
    with respx.mock() as mock:
        _login(mock)
        query = mock.post(QUERY_URL).respond(
            200, json=_cost_answer(_GROUPED_COLUMNS, _grouped_rows(2), next_link)
        )
        out = await _connector().cost_query(operator(), AzureTargetStub(), {})

    assert query.call_count == 1
    assert out["omissions"]["truncated"] is True
    assert out["omissions"]["kind"] == "page_limit"
    assert "Narrow the request" in out["omissions"]["message"]


@pytest.mark.asyncio
async def test_cost_query_refuses_a_next_link_of_another_subscription() -> None:
    other = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_B}/providers/Microsoft.CostManagement/query"
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(
            200, json=_cost_answer(_GROUPED_COLUMNS, _grouped_rows(1), f"{other}?$skiptoken=x")
        )
        elsewhere = mock.post(other).respond(200, json=_cost_answer(_GROUPED_COLUMNS, []))
        with pytest.raises(AzureUrlRefusedError):
            await _connector().cost_query(operator(), AzureTargetStub(), {})

    assert not elsewhere.called


@pytest.mark.asyncio
async def test_cost_query_rows_in_two_currencies_have_no_sum() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(
            200,
            json=_cost_answer(
                _GROUPED_COLUMNS,
                [[1.0, "rg-a", "Storage", "EUR"], [2.0, "rg-b", "Storage", "USD"]],
            ),
        )
        out = await _connector().cost_query(operator(), AzureTargetStub(), {})

    assert out["totals"] == {
        "cost": None,
        "currency": None,
        "currencies": ["EUR", "USD"],
        "rows": 2,
    }


@pytest.mark.asyncio
async def test_cost_query_with_no_content_returns_no_rows() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(204)
        out = await _connector().cost_query(operator(), AzureTargetStub(), {})

    assert out["items"] == []
    assert out["totals"]["rows"] == 0
    assert out["query"]["cost_column"] is None


@pytest.mark.asyncio
async def test_cost_query_without_a_cost_column_fails_and_guesses_nothing() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(
            200, json=_cost_answer([("Amount", "Number"), ("Currency", "String")], [[1.0, "EUR"]])
        )
        with pytest.raises(ValueError, match="no cost column"):
            await _connector().cost_query(operator(), AzureTargetStub(), {})


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"period": "last_month", "from": "2026-09-01", "to": "2026-09-30"}, "not both"),
        ({"from": "2026-09-01"}, "both from and to"),
        ({"from": "2026-09-30", "to": "2026-09-01"}, "not be after"),
        ({"from": "2026-02-30", "to": "2026-03-01"}, "YYYY-MM-DD"),
        ({"period": "last_year"}, "period must be one of"),
        ({"type": "billed"}, "type must be one of"),
        ({"granularity": "hourly"}, "granularity must be one of"),
        ({"group_by": ["service", "location", "meter"]}, "at most two"),
        ({"group_by": ["owner"]}, "group_by names are"),
        ({"group_by": ["service", "service"]}, "same thing twice"),
        ({"group_by": ["tag:a/b"]}, "group_by names are"),
        ({"resource_group": "rg-a/../other"}, "resource group name"),
        ({"resource_group": "rg-a."}, "resource group name"),
        ({"resource_group": "rg a"}, "resource group name"),
    ],
)
@pytest.mark.asyncio
async def test_cost_query_refuses_bad_parameters_before_any_call(
    params: dict[str, Any], message: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        query = mock.post(url__startswith=ARM_BASE).respond(200, json={})
        with pytest.raises(ValueError, match=message):
            await _connector().cost_query(operator(), AzureTargetStub(), params)

    assert not login.called
    assert not query.called


@pytest.mark.asyncio
async def test_a_long_cost_list_keeps_its_blocks_next_to_the_stored_result() -> None:
    """The real reducer with the operation's hint: the blocks survive at the handle."""
    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(200, json=_cost_answer(_GROUPED_COLUMNS, _grouped_rows(60)))
        out = await _connector().cost_query(
            operator(), AzureTargetStub(), {"period": "last_month", "group_by": ["resource_group"]}
        )

    op = OPS_BY_ID["azure.cost.query"]
    assert op.llm_instructions is not None
    context = {"op_id": op.op_id, "result_objects": op.llm_instructions["result_objects"]}
    summary, handle = await JsonFluxReducer().reduce(out, None, context)

    assert handle is not None
    assert summary["row_count"] == 60
    # The reducer's own "total" is the row count; the cost total is in totals.
    assert summary["total"] == 60
    assert summary["totals"] == {"cost": 75.0, "currency": "EUR", "currencies": ["EUR"], "rows": 60}
    assert summary["period"] == out["period"]
    assert summary["scope"] == out["scope"]
    assert summary["query"] == out["query"]
    assert summary["omissions"] == out["omissions"]


# ----------------------------------------------------------------------
# Errors that name the billing setting
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_401_on_a_cost_read_names_the_billing_settings() -> None:
    loader = CountingLoader()
    with respx.mock() as mock:
        login = _login(mock)
        query = mock.post(QUERY_URL).respond(401, json={"error": {"code": "Unauthorized"}})
        with pytest.raises(ConnectorAuthError) as caught:
            await _connector(loader).cost_query(operator(), AzureTargetStub(), {})

    error = caught.value
    assert error.status_code == 401
    assert login.call_count == 2  # one new login after the first 401, as for every call
    assert query.call_count == 2
    assert "Account owners can view charges" in str(error)
    assert "'Azure charges'" in str(error)
    assert error.remediation is not None and "Cost Management Reader" in error.remediation
    assert login_token() not in str(error)


@pytest.mark.asyncio
async def test_a_refused_login_during_a_cost_read_keeps_its_own_error() -> None:
    """A wrong secret is not a billing setting: Microsoft's answer stays."""
    refusal = {"error": "invalid_client", "error_codes": [7000215]}
    with respx.mock(assert_all_called=False) as mock:
        mock.post(token_url()).respond(401, json=refusal)
        query = mock.post(QUERY_URL).respond(200, json=_cost_answer(_GROUPED_COLUMNS, []))
        with pytest.raises(ConnectorAuthError) as caught:
            await _connector().cost_query(operator(), AzureTargetStub(), {})

    error = caught.value
    assert error.stage == "establish"
    assert "AADSTS7000215" in str(error)
    assert error.remediation is not None and "client_secret" in error.remediation
    assert "view charges" not in str(error)
    assert not query.called


@pytest.mark.parametrize(
    ("status", "code", "kind", "setting"),
    [
        (403, "AccountCostDisabled", AzureForbiddenError, "Account owners can view charges"),
        (403, "DepartmentCostDisabled", AzureForbiddenError, "Department admins can view charges"),
        (400, "IndirectCostDisabled", AzureRequestError, "partner"),
    ],
)
@pytest.mark.asyncio
async def test_an_agreement_setting_error_names_the_setting(
    status: int, code: str, kind: type[Exception], setting: str
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(BUDGETS_URL).respond(
            status, json={"error": {"code": code, "message": "Cost data is not available."}}
        )
        with pytest.raises(kind) as caught:
            await _connector().cost_budget_list(operator(), AzureTargetStub(), {})

    error = caught.value
    assert setting in str(error)
    assert getattr(error, "azure_error_code", None) == code
    assert getattr(error, "http_status", None) == status
    assert setting in str(getattr(error, "remediation", ""))


@pytest.mark.asyncio
async def test_an_unknown_cost_error_passes_through_unchanged() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(
            400, json={"error": {"code": "BadRequest", "message": "Invalid dataset."}}
        )
        with pytest.raises(AzureRequestError) as caught:
            await _connector().cost_query(operator(), AzureTargetStub(), {})

    assert "view charges" not in str(caught.value)
    assert caught.value.azure_error_code == "BadRequest"


@pytest.mark.asyncio
async def test_failed_dependency_on_a_query_gets_no_forecast_text() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(
            424, json={"error": {"code": "FailedDependency", "message": "Not available."}}
        )
        with pytest.raises(AzureRequestError) as caught:
            await _connector().cost_query(operator(), AzureTargetStub(), {})

    assert caught.value.azure_error_code == "FailedDependency"
    assert "forecast" not in str(caught.value)


# ----------------------------------------------------------------------
# azure.cost.forecast
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forecast_asks_for_this_month_and_adds_actual_and_forecast() -> None:
    columns = [
        ("PreTaxCost", "Number"),
        ("UsageDate", "Number"),
        ("CostStatus", "String"),
        ("Currency", "String"),
    ]
    rows = [
        [10.0, 20261014, "Actual", "EUR"],
        [11.0, 20261015, "Actual", "EUR"],
        [12.004, 20261016, "Forecast", "EUR"],
        [12.004, 20261017, "Forecast", "EUR"],
    ]
    with respx.mock() as mock:
        _login(mock)
        forecast = mock.post(FORECAST_URL).respond(200, json=_cost_answer(columns, rows))
        out = await _connector().cost_forecast(operator(), AzureTargetStub(), {})

    assert forecast.calls[0].request.url.params["api-version"] == "2026-06-01"
    assert _body(forecast) == {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": "2026-10-01T00:00:00Z", "to": "2026-10-31T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
        },
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }
    assert out["items"][2] == {
        "cost": 12.004,
        "date": "2026-10-16",
        "status": "Forecast",
        "currency": "EUR",
    }
    assert out["totals"] == {
        "actual_cost": 21.0,
        "forecast_cost": 24.01,
        "month_total": 45.01,
        "currency": "EUR",
        "currencies": ["EUR"],
        "rows": 4,
    }
    assert out["period"] == {
        "name": "current_month",
        "from": "2026-10-01",
        "to": "2026-10-31",
        "granularity": "daily",
    }


@pytest.mark.asyncio
async def test_a_forecast_without_enough_history_says_why() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(FORECAST_URL).respond(
            424, json={"error": {"code": "FailedDependency", "message": "No forecast."}}
        )
        with pytest.raises(AzureRequestError) as caught:
            await _connector().cost_forecast(operator(), AzureTargetStub(), {})

    assert "one week of cost data" in str(caught.value)


# ----------------------------------------------------------------------
# azure.cost.budget.list
# ----------------------------------------------------------------------


def _budget(
    name: str,
    amount: float,
    spent: float,
    forecast: float | None = None,
    *,
    scope: str = f"subscriptions/{SUBSCRIPTION_A}",
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "category": "Cost",
        "amount": amount,
        "timeGrain": "Monthly",
        "timePeriod": {"startDate": "2026-01-01T00:00:00Z", "endDate": "2027-12-31T00:00:00Z"},
        "currentSpend": {"amount": spent, "unit": "EUR"},
        "notifications": {
            "Actual_GreaterThan_80_Percent": {
                "enabled": True,
                "operator": "GreaterThan",
                "threshold": 80,
                "contactEmails": ["budget-owner@example.com"],
                "contactRoles": ["Owner"],
            }
        },
    }
    if forecast is not None:
        properties["forecastSpend"] = {"amount": forecast, "unit": "EUR"}
    return {
        "id": f"{scope}/providers/Microsoft.Consumption/budgets/{name}",
        "name": name,
        "type": "Microsoft.Consumption/budgets",
        "eTag": '"1d34d016a593709"',
        "properties": properties,
    }


@pytest.mark.asyncio
async def test_budgets_list_with_percent_used_and_without_mail_addresses() -> None:
    with respx.mock() as mock:
        _login(mock)
        budgets = mock.get(BUDGETS_URL).respond(
            200,
            json={"value": [_budget("monthly", 1000, 512.345, 980.5), _budget("empty", 0, 3.0)]},
        )
        out = await _connector().cost_budget_list(operator(), AzureTargetStub(), {})

    assert budgets.calls[0].request.url.params["api-version"] == "2026-06-01"
    assert out["budgets"][0] == {
        "name": "monthly",
        "resource_group": None,
        "category": "Cost",
        "amount": 1000.0,
        "time_grain": "Monthly",
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2027-12-31T00:00:00Z"},
        "current_spend": 512.345,
        "forecast_spend": 980.5,
        "percent_used": 51.23,
        "currency": "EUR",
    }
    assert out["budgets"][1]["percent_used"] is None
    assert out["budgets"][1]["forecast_spend"] is None
    assert out["count"] == 2
    assert out["scope"] == {"subscription_id": SUBSCRIPTION_A, "resource_group": None}
    assert "budget-owner@example.com" not in json.dumps(out)


@pytest.mark.asyncio
async def test_a_subscription_budget_list_says_which_budget_belongs_to_a_resource_group() -> None:
    """Azure lists both kinds at subscription scope, even with the same name."""
    group_scope = f"subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-dev"
    with respx.mock() as mock:
        _login(mock)
        mock.get(BUDGETS_URL).respond(
            200,
            json={
                "value": [
                    _budget("team", 100, 10, scope=group_scope),
                    _budget("team", 600, 60, scope=f"/subscriptions/{SUBSCRIPTION_A}"),
                ]
            },
        )
        out = await _connector().cost_budget_list(operator(), AzureTargetStub(), {})

    assert [(row["name"], row["resource_group"]) for row in out["budgets"]] == [
        ("team", "rg-dev"),
        ("team", None),
    ]


@pytest.mark.asyncio
async def test_budgets_of_one_resource_group() -> None:
    with respx.mock() as mock:
        _login(mock)
        budgets = mock.get(
            f"{SCOPE_URL}/resourceGroups/rg-a/providers/Microsoft.Consumption/budgets"
        ).respond(200, json={"value": [_budget("rg-budget", 200, 50)]})
        out = await _connector().cost_budget_list(
            operator(), AzureTargetStub(), {"resource_group": "rg-a"}
        )

    assert budgets.called
    assert out["budgets"][0]["percent_used"] == 25.0
    assert out["scope"]["resource_group"] == "rg-a"


@pytest.mark.asyncio
async def test_a_long_budget_list_keeps_count_scope_and_omissions_at_the_handle() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(BUDGETS_URL).respond(
            200, json={"value": [_budget(f"b{n}", 100, n) for n in range(55)]}
        )
        out = await _connector().cost_budget_list(operator(), AzureTargetStub(), {})

    op = OPS_BY_ID["azure.cost.budget.list"]
    assert op.llm_instructions is not None
    context = {"op_id": op.op_id, "result_objects": op.llm_instructions["result_objects"]}
    summary, handle = await JsonFluxReducer().reduce(out, None, context)

    assert handle is not None
    assert summary["source_key"] == "budgets"
    assert summary["row_count"] == 55
    assert summary["count"] == 55
    assert summary["scope"] == out["scope"]
    assert summary["omissions"] == out["omissions"]


# ----------------------------------------------------------------------
# azure.advisor.recommendation.list
# ----------------------------------------------------------------------


def _recommendation(
    n: int,
    *,
    category: str = "Cost",
    impact: str = "High",
    status: str | None = "New",
    metadata: bool = True,
) -> dict[str, Any]:
    group = f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-a"
    resource = f"{group}/providers/Microsoft.Compute/virtualMachines/vm-{n}"
    properties: dict[str, Any] = {
        "category": category,
        "impact": impact,
        "impactedField": "Microsoft.Compute/virtualMachines",
        "impactedValue": f"vm-{n}",
        "lastUpdated": "2026-10-14T08:00:00Z",
        "recommendationTypeId": "00000000-0000-0000-0000-0000000000aa",
        "risk": "Warning",
        "shortDescription": {
            "problem": "Right-size or shut down underused virtual machines",
            "solution": "Right-size or shut down underused virtual machines",
        },
        "extendedProperties": {
            "savingsAmount": "12.5",
            "annualSavingsAmount": "150",
            "savingsCurrency": "EUR",
        },
    }
    if status is not None:
        properties["recommendationStatus"] = status
    if metadata:
        properties["resourceMetadata"] = {"resourceId": resource, "singular": "Virtual machine"}
    return {
        "id": f"{resource}/providers/Microsoft.Advisor/recommendations/rec-{n}",
        "name": f"rec-{n}",
        "type": "Microsoft.Advisor/recommendations",
        "properties": properties,
    }


@pytest.mark.asyncio
async def test_advisor_sends_the_category_filter_and_filters_the_impact() -> None:
    answer = {
        "value": [
            _recommendation(1),
            _recommendation(2, impact="Low"),
            _recommendation(3, metadata=False),
        ]
    }
    with respx.mock() as mock:
        _login(mock)
        advisor = mock.get(ADVISOR_URL).respond(200, json=answer)
        out = await _connector().advisor_recommendation_list(
            operator(), AzureTargetStub(), {"category": "Cost", "impact": "High"}
        )

    params = advisor.calls[0].request.url.params
    assert params["api-version"] == "2026-10-01"
    assert params["$filter"] == "Category eq 'Cost'"
    assert out["count"] == 2
    assert [item["id"] for item in out["items"]] == ["rec-1", "rec-3"]
    first = out["items"][0]
    assert first["savings_amount"] == 12.5
    assert first["annual_savings_amount"] == 150.0
    assert first["savings_currency"] == "EUR"
    assert first["problem"] == "Right-size or shut down underused virtual machines"
    resource = f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-a/providers/Microsoft.Compute"
    assert out["items"][1]["resource_id"] == f"{resource}/virtualMachines/vm-3"
    assert out["filters"] == {"category": "Cost", "impact": "High", "inactive_left_out": 0}


@pytest.mark.asyncio
async def test_advisor_leaves_out_dismissed_postponed_and_completed_and_counts_them() -> None:
    answer = {
        "value": [
            _recommendation(1),
            _recommendation(2, status="Dismissed"),
            _recommendation(3, status="Postponed"),
            _recommendation(4, status="Completed"),
            _recommendation(5, status=None),
        ]
    }
    with respx.mock() as mock:
        _login(mock)
        advisor = mock.get(ADVISOR_URL).respond(200, json=answer)
        out = await _connector().advisor_recommendation_list(operator(), AzureTargetStub(), {})

    assert "$filter" not in advisor.calls[0].request.url.params
    assert [item["id"] for item in out["items"]] == ["rec-1", "rec-5"]
    assert out["count"] == 2
    assert out["filters"]["inactive_left_out"] == 3


@pytest.mark.asyncio
async def test_advisor_refuses_an_unknown_category() -> None:
    with pytest.raises(ValueError, match="category must be one of"):
        await _connector().advisor_recommendation_list(
            operator(), AzureTargetStub(), {"category": "Cost' or Category eq 'Security"}
        )


@pytest.mark.asyncio
async def test_a_long_advisor_list_keeps_count_and_filters_at_the_handle() -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ADVISOR_URL).respond(200, json={"value": [_recommendation(n) for n in range(70)]})
        out = await _connector().advisor_recommendation_list(
            operator(), AzureTargetStub(), {"impact": "High"}
        )

    op = OPS_BY_ID["azure.advisor.recommendation.list"]
    assert op.llm_instructions is not None
    context = {"op_id": op.op_id, "result_objects": op.llm_instructions["result_objects"]}
    summary, handle = await JsonFluxReducer().reduce(out, None, context)

    assert handle is not None
    assert summary["row_count"] == 70
    assert summary["count"] == 70
    assert summary["filters"] == {"category": None, "impact": "High", "inactive_left_out": 0}
    assert summary["scope"] == {"subscription_id": SUBSCRIPTION_A}
    assert summary["omissions"] == out["omissions"]


# ----------------------------------------------------------------------
# azure.price.list
# ----------------------------------------------------------------------


def _price(n: int, *, price_type: str = "Consumption", term: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "currencyCode": "EUR",
        "tierMinimumUnits": 0.0,
        "retailPrice": 0.25,
        "unitPrice": 0.25,
        "armRegionName": "westeurope",
        "location": "EU West",
        "effectiveStartDate": "2026-01-01T00:00:00Z",
        "meterId": f"00000000-0000-0000-0000-{n:012d}",
        "meterName": "D4s v5",
        "productId": "EXAMPLEPRODUCT",
        "skuId": "EXAMPLEPRODUCT/0001",
        "productName": "Virtual Machines Dsv5 Series",
        "skuName": "D4s v5",
        "serviceName": "Virtual Machines",
        "serviceId": "EXAMPLESERVICE",
        "serviceFamily": "Compute",
        "unitOfMeasure": "1 Hour",
        "type": price_type,
        "isPrimaryMeterRegion": True,
        "armSkuName": "Standard_D4s_v5",
    }
    if term is not None:
        item["reservationTerm"] = term
    return item


def _price_page(items: list[dict[str, Any]], next_link: str | None = None) -> dict[str, Any]:
    return {
        "BillingCurrency": "USD",
        "CustomerEntityId": "Default",
        "CustomerEntityType": "Retail",
        "Items": items,
        "NextPageLink": next_link,
        "Count": len(items),
    }


async def _refuse_any_secret_read(_target: Any, _operator: Any) -> Any:
    raise AssertionError("the price list must not read the target's secret")


@pytest.mark.asyncio
async def test_price_list_never_sends_a_token() -> None:
    """An ARM call caches a token first; the price list still gets none."""
    loader = CountingLoader()
    connector = _connector(loader)
    with respx.mock() as mock:
        login = _login(mock)
        mock.get(subscription_url()).respond(200, json=subscription_answer())
        prices = mock.get(PRICES_URL).respond(200, json=_price_page([_price(1)]))
        await connector.about(operator(), AzureTargetStub(), {})
        out = await connector.price_list(
            operator(), AzureTargetStub(), {"sku": "Standard_D4s_v5", "region": "westeurope"}
        )

    request = prices.calls[0].request
    assert "authorization" not in {name.lower() for name in request.headers}
    assert login_token() not in json.dumps(dict(request.headers))
    assert login_token() not in str(request.url)
    assert login.call_count == 1
    assert loader.calls == 1
    assert out["items"][0]["arm_sku_name"] == "Standard_D4s_v5"


@pytest.mark.asyncio
async def test_price_list_reads_no_secret_and_needs_no_login() -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(url__startswith="https://login.microsoftonline.com").respond(500)
        mock.get(PRICES_URL).respond(200, json=_price_page([_price(1)]))
        out = await _connector(_refuse_any_secret_read).price_list(
            operator(), AzureTargetStub(), {"service_name": "Virtual Machines"}
        )

    assert not login.called
    assert out["lookup"]["rows"] == 1


@pytest.mark.asyncio
async def test_price_list_builds_and_quotes_the_filter() -> None:
    params = {
        "service_name": "Virtual Machines",
        "region": "WestEurope",
        "sku": "Standard_D4s_v5",
        "price_type": "Reservation",
        "product_name": "It's a name",
        "meter_name": "D4s v5",
    }
    with respx.mock() as mock:
        prices = mock.get(PRICES_URL).respond(
            200, json=_price_page([_price(1, price_type="Reservation", term="3 Years")])
        )
        out = await _connector().price_list(operator(), AzureTargetStub(), params)

    query = prices.calls[0].request.url.params
    assert query["$filter"] == (
        "serviceName eq 'Virtual Machines' and armRegionName eq 'westeurope' and "
        "armSkuName eq 'Standard_D4s_v5' and priceType eq 'Reservation' and "
        "productName eq 'It''s a name' and meterName eq 'D4s v5'"
    )
    assert query["currencyCode"] == "'EUR'"
    assert query["api-version"] == "2023-01-01-preview"
    assert "$skip" not in query
    assert out["items"] == [
        {
            "service_name": "Virtual Machines",
            "product_name": "Virtual Machines Dsv5 Series",
            "sku_name": "D4s v5",
            "arm_sku_name": "Standard_D4s_v5",
            "meter_id": "00000000-0000-0000-0000-000000000001",
            "meter_name": "D4s v5",
            "region": "westeurope",
            "location": "EU West",
            "unit": "1 Hour",
            "retail_price": 0.25,
            "unit_price": 0.25,
            "price_type": "Reservation",
            "reservation_term": "3 Years",
            "effective_start_date": "2026-01-01T00:00:00Z",
            "currency": "EUR",
            "tier_minimum_units": 0.0,
            "is_primary_meter_region": True,
        }
    ]
    lookup = out["lookup"]
    assert lookup["currency"] == "EUR"
    assert lookup["reference_only"] is True
    assert "not quotes" in lookup["note"]
    assert lookup["filter"] == query["$filter"]
    assert lookup["looked_up_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_price_list_reads_the_next_page_with_its_own_query_and_the_skip_only() -> None:
    """The next link drops the currency; MEHO asks again with its own query."""
    next_link = (
        "https://prices.azure.com:443/api/retail/prices"
        "?$filter=serviceName%20eq%20%27Other%27&$skip=1000"
    )
    with respx.mock() as mock:
        prices = mock.get(PRICES_URL).mock(
            side_effect=[
                httpx.Response(200, json=_price_page([_price(1)], next_link)),
                httpx.Response(200, json=_price_page([_price(2)])),
            ]
        )
        out = await _connector().price_list(
            operator(), AzureTargetStub(), {"sku": "Standard_D4s_v5", "currency": "USD"}
        )

    second = prices.calls[1].request.url.params
    assert second["$skip"] == "1000"
    assert second["$filter"] == "armSkuName eq 'Standard_D4s_v5'"
    assert second["currencyCode"] == "'USD'"
    assert len(out["items"]) == 2
    assert out["lookup"]["reference_only"] is False
    assert out["omissions"]["pages_read"] == 2


@pytest.mark.parametrize(
    "link",
    [
        "https://attacker.example.net/api/retail/prices?$skip=1000",
        "http://prices.azure.com/api/retail/prices?$skip=1000",
        "https://prices.azure.com/api/other?$skip=1000",
        "https://prices.azure.com/api/retail/prices?$skip=0",
        "https://prices.azure.com/api/retail/prices?$skiptoken=abc",
    ],
)
@pytest.mark.asyncio
async def test_price_list_refuses_a_next_page_link_it_cannot_trust(link: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        prices = mock.get(PRICES_URL).respond(200, json=_price_page([_price(1)], link))
        other = mock.get(url__startswith="https://attacker.example.net").respond(200)
        with pytest.raises(AzureUrlRefusedError):
            await _connector().price_list(operator(), AzureTargetStub(), {"sku": "Standard_D4s_v5"})

    assert prices.call_count == 1
    assert not other.called


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "at least one filter"),
        ({"currency": "EUR"}, "at least one filter"),
        ({"sku": "Standard_D4s_v5", "currency": "XYZ"}, "currency must be one of"),
        ({"sku": "Standard_D4s_v5", "price_type": "Spot"}, "price_type must be one of"),
        ({"sku": "Standard_D4s_v5\nand x eq 'y'"}, "without line breaks"),
        ({"sku": "x" * 201}, "1 to 200"),
    ],
)
@pytest.mark.asyncio
async def test_price_list_refuses_bad_parameters_before_any_call(
    params: dict[str, Any], message: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        prices = mock.get(PRICES_URL).respond(200, json=_price_page([]))
        with pytest.raises(ValueError, match=message):
            await _connector().price_list(operator(), AzureTargetStub(), params)

    assert not prices.called


@pytest.mark.asyncio
async def test_a_throttled_price_list_says_when_to_try_again() -> None:
    with respx.mock() as mock:
        mock.get(PRICES_URL).respond(429, headers={"Retry-After": "30"})
        with pytest.raises(AzureThrottledError) as caught:
            await _connector().price_list(operator(), AzureTargetStub(), {"sku": "Standard_D4s_v5"})

    assert caught.value.retry_after_seconds == 30
    assert "in 30 seconds" in str(caught.value)


@pytest.mark.asyncio
async def test_a_price_list_redirect_is_not_followed() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(PRICES_URL).respond(302, headers={"Location": "https://attacker.example.net/"})
        other = mock.get(url__startswith="https://attacker.example.net").respond(200)
        with pytest.raises(AzureUrlRefusedError):
            await _connector().price_list(operator(), AzureTargetStub(), {"sku": "Standard_D4s_v5"})

    assert not other.called


@pytest.mark.asyncio
async def test_a_long_price_list_keeps_the_lookup_at_the_handle() -> None:
    with respx.mock() as mock:
        mock.get(PRICES_URL).respond(200, json=_price_page([_price(n) for n in range(120)]))
        out = await _connector().price_list(
            operator(), AzureTargetStub(), {"service_name": "Virtual Machines"}
        )

    op = OPS_BY_ID["azure.price.list"]
    assert op.llm_instructions is not None
    context = {"op_id": op.op_id, "result_objects": op.llm_instructions["result_objects"]}
    summary, handle = await JsonFluxReducer().reduce(out, None, context)

    assert handle is not None
    assert summary["row_count"] == 120
    assert summary["lookup"] == out["lookup"]
    assert summary["omissions"] == out["omissions"]


# ----------------------------------------------------------------------
# Registration and the governed call
# ----------------------------------------------------------------------


@pytest.fixture
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


class _RecordingStore:
    """Stand-in for the stored-result store: keeps what the reducer stores."""

    def __init__(self) -> None:
        self.spills: list[dict[str, Any]] = []

    async def spill(self, *, rows: list[dict[str, Any]], **kwargs: Any) -> SpillResult:
        self.spills.append({"rows": rows, **kwargs})
        return SpillResult(
            stored_rows=len(rows), document_stored=kwargs.get("document") is not None
        )


def test_every_cost_operation_is_a_safe_read_in_the_cost_group() -> None:
    assert [op.op_id for op in OPS] == [
        "azure.cost.query",
        "azure.cost.forecast",
        "azure.cost.budget.list",
        "azure.advisor.recommendation.list",
        "azure.price.list",
    ]
    assert {op.op_id for op in OPS} <= {op.op_id for op in AZURE_TYPED_OPS}
    assert "cost" in AZURE_TYPED_WHEN_TO_USE_BY_GROUP
    for op in OPS:
        assert op.group_key == "cost"
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert op.parameter_schema["additionalProperties"] is False
        assert op.llm_instructions is not None
        assert "result_objects" in op.llm_instructions
        assert hasattr(AzureConnector, op.handler_attr)


@pytest.mark.usefixtures("_azure_only_registry")
@pytest.mark.asyncio
async def test_the_cost_operations_are_stored_as_typed_reads_with_their_group(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_azure_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == AZURE_PRODUCT,
                    EndpointDescriptor.impl_id == AZURE_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    by_op = {row.op_id: row for row in rows}
    for op in OPS:
        row = by_op[op.op_id]
        assert row.source_kind == "typed"
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert row.handler_ref is not None
        assert row.handler_ref.endswith(f"CostOps.{op.handler_attr}")
        assert row.llm_instructions == op.llm_instructions
    group = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.impl_id == AZURE_IMPL_ID, OperationGroup.group_key == "cost"
                )
            )
        )
        .scalars()
        .one()
    )
    assert group.review_status == "enabled"
    assert group.when_to_use == AZURE_TYPED_WHEN_TO_USE_BY_GROUP["cost"]


@pytest.mark.usefixtures("_azure_only_registry")
@pytest.mark.asyncio
async def test_a_governed_long_cost_query_returns_a_link_with_its_blocks_inline(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """Through the dispatcher: the hint comes from the stored operation."""
    store = _RecordingStore()
    set_default_reducer(JsonFluxReducer(store=store))  # type: ignore[arg-type]
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()

    with respx.mock() as mock:
        _login(mock)
        mock.post(QUERY_URL).respond(200, json=_cost_answer(_GROUPED_COLUMNS, _grouped_rows(60)))
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.cost.query",
            target=AzureTargetStub(),
            params={"period": "last_month", "group_by": ["resource_group"]},
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.handle.total_rows == 60
    summary = result.result
    assert isinstance(summary, dict)
    assert summary["totals"]["cost"] == 75.0
    assert summary["period"]["from"] == "2026-09-01"
    assert summary["scope"]["subscription_id"] == SUBSCRIPTION_A
    assert summary["omissions"]["truncated"] is False
    assert login_token() not in json.dumps(result.model_dump(mode="json"))
    assert len(store.spills) == 1
    assert len(store.spills[0]["rows"]) == 60
    assert store.spills[0]["document"]["totals"]["cost"] == 75.0


@pytest.mark.usefixtures("_azure_only_registry")
@pytest.mark.asyncio
async def test_a_governed_call_refuses_parameters_outside_the_schema(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_azure_typed_operations()

    with respx.mock(assert_all_called=False) as mock:
        arm = mock.route(url__startswith=ARM_BASE).respond(200, json={})
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.cost.query",
            target=AzureTargetStub(),
            params={"group_by": ["resource_group", "service", "meter"], "currency": "EUR"},
        )

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not arm.called
