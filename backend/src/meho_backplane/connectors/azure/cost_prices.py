# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``azure.price.list``: public Azure retail prices, with no login.

The Azure Retail Prices API (``prices.azure.com``) is public. It is not Azure
Resource Manager: it needs no login, and MEHO never sends a token there. So
this call does not use :func:`~meho_backplane.connectors.azure._http.send`,
which adds the target's token. It uses its own short-lived HTTP client with
no ``Authorization`` header, and it follows no redirect.

MEHO builds the filter text itself from structured parameters and quotes
every value, so a caller cannot add filter text of its own. From each
``NextPageLink`` MEHO reads only the ``$skip`` number and asks for the next
page with its own query (currency, filter, API version), so a next-page link
can never send the call to another address.

Microsoft's prices are in US dollars. Prices in other currencies are
converted reference values, not quotes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import (
    _read_capped_json_response,
    json_payload_or_empty,
)
from meho_backplane.connectors.azure._errors import (
    AzureRequestError,
    AzureServerError,
    AzureThrottledError,
    AzureUrlRefusedError,
    azure_error_body,
    clean_azure_text,
)
from meho_backplane.connectors.azure._http import retry_after_seconds
from meho_backplane.connectors.azure._paging import PageLimits, _Pager
from meho_backplane.connectors.azure._paths import RETAIL_PRICES_API_VERSION, RETAIL_PRICES_URL
from meho_backplane.connectors.azure.cost_reads import omissions_block
from meho_backplane.connectors.azure.session import AzureTargetLike
from meho_backplane.flight_recorder import capture as flight_recorder_capture

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["PRICE_CURRENCIES", "PRICE_FILTERS", "PRICE_TYPES", "price_filter", "price_list_impl"]

#: The currencies the price list answers in. USD is Microsoft's own price;
#: the others are converted from USD.
PRICE_CURRENCIES: Final[tuple[str, ...]] = (
    "USD",
    "AUD",
    "BRL",
    "CAD",
    "CHF",
    "CNY",
    "DKK",
    "EUR",
    "GBP",
    "INR",
    "JPY",
    "KRW",
    "NOK",
    "NZD",
    "RUB",
    "SEK",
    "TWD",
)

#: The filter parameters and the price list's field for each.
PRICE_FILTERS: Final[Mapping[str, str]] = {
    "service_name": "serviceName",
    "region": "armRegionName",
    "sku": "armSkuName",
    "price_type": "priceType",
    "product_name": "productName",
    "meter_name": "meterName",
}

#: The price types: pay-as-you-go, reservation, and Dev/Test pay-as-you-go.
PRICE_TYPES: Final[tuple[str, ...]] = ("Consumption", "Reservation", "DevTestConsumption")

_HOST: Final[str] = httpx.URL(RETAIL_PRICES_URL).host
_PATH: Final[str] = httpx.URL(RETAIL_PRICES_URL).path
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
_MAX_VALUE_LENGTH: Final[int] = 200
_CONTROL: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
_CODE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,99}")
_HEADERS: Final[dict[str, str]] = {"Accept": "application/json"}

_USD_NOTE: Final[str] = "USD prices are Microsoft's retail prices, without discounts."
_OTHER_NOTE: Final[str] = (
    "Prices in {currency} are reference values converted from US dollars, not quotes. "
    "An invoice can differ."
)


def _quoted(name: str, value: object) -> str:
    """*value* as a quoted filter literal. A single quote inside is doubled."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_VALUE_LENGTH
        or _CONTROL.search(value)
    ):
        raise ValueError(
            f"{name} must be a text of 1 to {_MAX_VALUE_LENGTH} characters, without line breaks."
        )
    return "'" + value.replace("'", "''") + "'"


def price_filter(params: Mapping[str, Any]) -> str:
    """The ``$filter`` text for *params*. At least one filter is needed."""
    price_type = params.get("price_type")
    if price_type is not None and price_type not in PRICE_TYPES:
        raise ValueError(f"price_type must be one of {', '.join(PRICE_TYPES)}.")
    parts: list[str] = []
    for name, field in PRICE_FILTERS.items():
        value = params.get(name)
        if value is None:
            continue
        if name == "region" and isinstance(value, str):
            value = value.lower()  # region names are lower case in the price list
        parts.append(f"{field} eq {_quoted(name, value)}")
    if not parts:
        raise ValueError(
            "Give at least one filter: service_name, region, sku, price_type, "
            "product_name or meter_name. The full price list has hundreds of "
            "thousands of rows."
        )
    return " and ".join(parts)


def _currency(params: Mapping[str, Any]) -> str:
    currency = params.get("currency") or "EUR"
    if currency not in PRICE_CURRENCIES:
        raise ValueError(f"currency must be one of {', '.join(PRICE_CURRENCIES)}.")
    return str(currency)


def _next_skip(link: object, skip: int) -> int | None:
    """The ``$skip`` of the next page from Azure's ``NextPageLink``, or ``None`` at the end.

    The link must point at the price list itself, over https, with a
    ``$skip`` greater than the current one. Otherwise the call fails: MEHO
    does not guess where the next page is.
    """
    if not isinstance(link, str) or not link:
        return None
    try:
        url = httpx.URL(link)
    except (httpx.InvalidURL, TypeError, ValueError):
        url = None
    if (
        url is None
        or url.scheme != "https"
        or url.host != _HOST
        or url.port not in (None, 443)
        or url.path != _PATH
    ):
        raise AzureUrlRefusedError(
            "The price list's next-page link points to another address. MEHO did not follow it."
        )
    raw = url.params.get("$skip", "")
    if not raw.isdigit() or int(raw) <= skip:
        raise AzureUrlRefusedError(
            "The price list's next-page link has no usable $skip number. MEHO stopped reading."
        )
    return int(raw)


def _error_text(response: httpx.Response) -> tuple[str | None, str | None]:
    """The price list's error code and message, cleaned. It may write them capitalised."""
    code, message = azure_error_body(response)
    if code or message:
        return code, message
    try:
        body = response.json()
    except ValueError:
        return None, None
    error = body.get("Error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None
    raw_code = error.get("Code")
    clean_code = raw_code if isinstance(raw_code, str) and _CODE.fullmatch(raw_code) else None
    return clean_code, clean_azure_text(error.get("Message"))


def _price_error(response: httpx.Response) -> Exception:
    """The error for an answer of the price list that is not a success."""
    status = response.status_code
    if status < 400:
        return AzureUrlRefusedError(
            f"The price list answered {status} with a redirect. MEHO follows no redirect."
        )
    code, message = _error_text(response)
    common: dict[str, Any] = {
        "http_status": status,
        "azure_error_code": code,
        "azure_message": message,
    }
    if status == 429:
        wait = retry_after_seconds(response)
        when = f"in {wait:g} seconds" if wait is not None else "in a minute"
        remediation = f"The public price list is limiting calls. Try again {when}."
        return AzureThrottledError(
            f"Azure price list: too many calls (HTTP 429). {remediation}",
            remediation=remediation,
            retry_after_seconds=wait,
            **common,
        )
    if status >= 500:
        remediation = "The public price list had a problem on its side. Try again later."
        return AzureServerError(
            f"Azure price list: HTTP {status}. {remediation}", remediation=remediation, **common
        )
    remediation = (
        "Check the filter values. They must match the price list exactly, in upper and "
        "lower case, for example service_name 'Virtual Machines' or sku 'Standard_D4s_v5'."
    )
    return AzureRequestError(
        f"Azure price list: the request was refused (HTTP {status}). {remediation}",
        remediation=remediation,
        **common,
    )


async def _read_page(client: httpx.AsyncClient, query: dict[str, str]) -> dict[str, Any]:
    """GET one page of the price list, with no token, and record it in the trace."""
    span = flight_recorder_capture.span_start()
    try:
        response = await _read_capped_json_response(
            client, "GET", RETAIL_PRICES_URL, params=query, headers=dict(_HEADERS)
        )
    except httpx.TransportError as exc:
        flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
        raise
    flight_recorder_capture.record_vendor_call(
        span, method="GET", request_headers=_HEADERS, response=response
    )
    if response.status_code >= 300:
        raise _price_error(response)
    try:
        body = json_payload_or_empty(response)
    except ValueError as exc:
        raise ValueError("The price list answered with something that is not JSON.") from exc
    if not isinstance(body, dict):
        raise ValueError("The price list answered with something that is not a JSON object.")
    return body


def _price_row(item: object) -> dict[str, Any] | None:
    """One price. The names and texts come from Azure: data, never instructions."""
    if not isinstance(item, dict):
        return None
    return {
        "service": item.get("serviceName"),
        "product": item.get("productName"),
        "sku": item.get("skuName"),
        "arm_sku": item.get("armSkuName"),
        "meter_id": item.get("meterId"),
        "meter_name": item.get("meterName"),
        "region": item.get("armRegionName"),
        "location": item.get("location"),
        "unit": item.get("unitOfMeasure"),
        "retail_price": item.get("retailPrice"),
        "unit_price": item.get("unitPrice"),
        "price_type": item.get("type"),
        "reservation_term": item.get("reservationTerm"),
        "effective_start_date": item.get("effectiveStartDate"),
        "currency": item.get("currencyCode"),
        "tier_minimum_units": item.get("tierMinimumUnits"),
        "is_primary_meter_region": item.get("isPrimaryMeterRegion"),
    }


async def price_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.price.list``: ``GET https://prices.azure.com/api/retail/prices``.

    Looks up public retail prices with MEHO's own filter. Needs no login:
    the target's login and token are not used, and no token is sent.
    """
    del connector, operator, target  # the price list is public: nothing of the target is used
    currency = _currency(params)
    filter_text = price_filter(params)
    looked_up_at = datetime.now(UTC).isoformat(timespec="seconds")
    pager = _Pager(PageLimits())
    skip = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        while True:
            query = {
                "api-version": RETAIL_PRICES_API_VERSION,
                "currencyCode": f"'{currency}'",
                "$filter": filter_text,
            }
            if skip:
                query["$skip"] = str(skip)
            answer = await _read_page(client, query)
            items = answer.get("Items")
            found = items if isinstance(items, list) else []
            rows = [row for row in map(_price_row, found) if row is not None]
            if not pager.add(rows):
                break
            next_skip = _next_skip(answer.get("NextPageLink"), skip)
            if next_skip is None or not pager.may_read_more():
                break
            skip = next_skip
    return {
        "items": pager.result.rows,
        "lookup": {
            "looked_up_at": looked_up_at,
            "currency": currency,
            "reference_only": currency != "USD",
            "note": _USD_NOTE if currency == "USD" else _OTHER_NOTE.format(currency=currency),
            "filter": filter_text,
            "api_version": RETAIL_PRICES_API_VERSION,
            "rows": len(pager.result.rows),
        },
        "omissions": omissions_block(pager.result),
    }
