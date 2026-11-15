# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A product answer of HTTP 410 says the product version no longer has the API."""

from __future__ import annotations

from typing import Any

import httpx

from meho_backplane.operations._errors import result_connector_http_410
from meho_backplane.operations.dispatcher import _classify_http_status_error


def _status_error(
    status_code: int,
    *,
    json_body: dict[str, Any] | None = None,
    text_body: str | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://product.example/api/old/path")
    kwargs: dict[str, Any] = {"request": request}
    if json_body is not None:
        kwargs["json"] = json_body
    elif text_body is not None:
        kwargs["text"] = text_body
    response = httpx.Response(status_code, **kwargs)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected a non-2xx response")


def _classify(exc: httpx.HTTPStatusError) -> Any:
    return _classify_http_status_error(
        http_exc=exc,
        op_id="GET:/api/old/path",
        connector_instance=None,
        target=None,
        duration_ms=1.0,
    )


def test_410_says_the_product_version_no_longer_has_the_api() -> None:
    exc = _status_error(410, json_body={"message": "This API is no longer supported"})
    out = _classify(exc)

    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("connector_http_410:")
    assert "this product version no longer has this API" in out.error
    assert "This API is no longer supported" in out.error
    assert out.extras["error_code"] == "connector_http_410"
    assert out.extras["http_status"] == 410
    assert out.extras["upstream_message"] == "This API is no longer supported"


def test_410_with_empty_body_still_gives_the_message() -> None:
    out = result_connector_http_410("GET:/x", _status_error(410), duration_ms=1.0)

    assert out.error is not None
    assert "this product version no longer has this API" in out.error
    assert out.extras["upstream_message"] is None


def test_410_text_body_is_passed_on() -> None:
    out = result_connector_http_410(
        "GET:/x", _status_error(410, text_body="gone for good"), duration_ms=1.0
    )

    assert out.error is not None
    assert "gone for good" in out.error


def test_other_statuses_are_unchanged() -> None:
    out_500 = _classify(_status_error(500, json_body={"message": "boom"}))
    assert out_500.extras["error_code"] == "connector_error"
    out_404 = _classify(_status_error(404))
    assert out_404.extras["error_code"] == "connector_error"
    out_422 = _classify(_status_error(422, json_body={"message": "bad"}))
    assert out_422.extras["error_code"] == "connector_http_422"
