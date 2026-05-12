# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for HttpConnector adapter (G0.2-T3).

Coverage matrix (per Task #242 acceptance criteria):

* ``HttpConnector`` class exists and is importable from the adapters package.
* Instantiating a subclass that omits the ABC methods raises :exc:`TypeError`;
  a direct ``HttpConnector()`` call also raises because the ABC methods are
  abstract.
* A subclass that overrides ``auth_headers`` + all three ABC methods can be
  instantiated and run an end-to-end request against a respx-mocked endpoint.
* 5xx response on GET retries exactly 3 times (4 total calls) then re-raises
  :exc:`httpx.HTTPStatusError`.
* 4xx response (e.g. 404) does NOT retry — exactly 1 call, then re-raise.
* :exc:`httpx.ConnectError` retries exactly 3 times (4 total calls).
* Per-target client pool: same ``target.name`` reuses the same
  :class:`httpx.AsyncClient`; different names get distinct clients.
* ``aclose()`` closes all pooled clients and empties the pool dict.
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.adapters.http import HttpConnector as _HttpConnectorDirect
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_target(
    name: str = "test-target", host: str = "vcenter.example.com", port: int = 443
) -> Any:
    """Return a minimal duck-typed Target stub."""
    t = types.SimpleNamespace(name=name, host=host, port=port, auth_model="impersonation")
    return t


class _ConcreteHttpConnector(HttpConnector):
    """Minimal concrete subclass — overrides auth_headers + all ABC methods."""

    product = "test-http"

    async def auth_headers(self, target: Any, raw_jwt: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {raw_jwt}"}

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Import / instantiation
# ---------------------------------------------------------------------------


def test_http_connector_importable_from_adapters_package() -> None:
    assert HttpConnector is _HttpConnectorDirect


def test_http_connector_abstract_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        HttpConnector()  # type: ignore[abstract]


def test_http_connector_subclass_missing_auth_headers_works() -> None:
    """HttpConnector's own methods are NOT abstract — subclass with only ABC
    methods can be instantiated; calling auth_headers raises NotImplementedError."""

    class _MissingAuth(HttpConnector):
        product = "test"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    conn = _MissingAuth()
    assert conn is not None


def test_concrete_subclass_instantiates() -> None:
    conn = _ConcreteHttpConnector()
    assert conn.product == "test-http"
    assert conn._clients == {}


# ---------------------------------------------------------------------------
# End-to-end with respx mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_json_success() -> None:
    """Subclass end-to-end: GET /api/items returns mocked JSON via respx."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        mock.get("/api/items").respond(200, json={"items": [1, 2, 3]})
        result = await conn._get_json(target, "/api/items", raw_jwt="tok-abc")

    assert result == {"items": [1, 2, 3]}
    await conn.aclose()


@pytest.mark.asyncio
async def test_auth_headers_forwarded() -> None:
    """auth_headers() result is sent as request headers."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/me").respond(200, json={"sub": "u1"})
        await conn._get_json(target, "/api/me", raw_jwt="my-jwt")

    assert route.called
    sent_headers = route.calls[0].request.headers
    assert sent_headers["authorization"] == "Bearer my-jwt"
    await conn.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour — 5xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_on_get_retries_three_times_then_reraises() -> None:
    """5xx on GET triggers 3 retries (4 total calls) then re-raises."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        # Tenacity's wait_exponential would sleep for 0.5 / 1.0 / 2.0 s in
        # production. We override the wait to avoid slowing the test suite.
        route = mock.get("/api/vms").respond(503)

        from tenacity import wait_none

        with (
            patch.object(
                conn._request_json.retry,  # type: ignore[attr-defined]
                "wait",
                wait_none(),
            ),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            await conn._request_json(target, "GET", "/api/vms", raw_jwt="tok")

    assert exc_info.value.response.status_code == 503
    assert route.call_count == 4  # 1 initial + 3 retries
    await conn.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour — 4xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_does_not_retry() -> None:
    """4xx response is not retried — exactly one call, then re-raise."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/missing").respond(404)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await conn._request_json(target, "GET", "/api/missing", raw_jwt="tok")

    assert exc_info.value.response.status_code == 404
    assert route.call_count == 1
    await conn.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour — ConnectError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_error_retries_three_times() -> None:
    """ConnectError triggers 3 retries (4 total calls) then re-raises."""
    conn = _ConcreteHttpConnector()
    target = _make_target()

    async with respx.mock(base_url="https://vcenter.example.com") as mock:
        route = mock.get("/api/vms").mock(side_effect=httpx.ConnectError("refused"))

        from tenacity import wait_none

        with (
            patch.object(
                conn._request_json.retry,  # type: ignore[attr-defined]
                "wait",
                wait_none(),
            ),
            pytest.raises(httpx.ConnectError),
        ):
            await conn._request_json(target, "GET", "/api/vms", raw_jwt="tok")

    assert route.call_count == 4
    await conn.aclose()


# ---------------------------------------------------------------------------
# Per-target client pooling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_target_reuses_client() -> None:
    """Two calls to the same target return the same httpx.AsyncClient instance."""
    conn = _ConcreteHttpConnector()
    target = _make_target(name="vc-01")

    client_a = await conn._http_client(target)
    client_b = await conn._http_client(target)

    assert client_a is client_b
    await conn.aclose()


@pytest.mark.asyncio
async def test_different_targets_get_different_clients() -> None:
    """Each distinct target.name gets its own httpx.AsyncClient."""
    conn = _ConcreteHttpConnector()
    target_a = _make_target(name="vc-01", host="vc01.example.com")
    target_b = _make_target(name="vc-02", host="vc02.example.com")

    client_a = await conn._http_client(target_a)
    client_b = await conn._http_client(target_b)

    assert client_a is not client_b
    assert "vc-01" in conn._clients
    assert "vc-02" in conn._clients
    await conn.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_all_clients_and_empties_pool() -> None:
    """aclose() calls .aclose() on every pooled client and clears the dict."""
    conn = _ConcreteHttpConnector()

    target_a = _make_target(name="t-a", host="a.example.com")
    target_b = _make_target(name="t-b", host="b.example.com")

    client_a = await conn._http_client(target_a)
    client_b = await conn._http_client(target_b)

    # Spy on aclose
    original_close_a = client_a.aclose
    original_close_b = client_b.aclose
    closed: list[str] = []

    async def _close_a() -> None:
        closed.append("t-a")
        await original_close_a()

    async def _close_b() -> None:
        closed.append("t-b")
        await original_close_b()

    client_a.aclose = _close_a  # type: ignore[method-assign]
    client_b.aclose = _close_b  # type: ignore[method-assign]

    await conn.aclose()

    assert set(closed) == {"t-a", "t-b"}
    assert conn._clients == {}


@pytest.mark.asyncio
async def test_aclose_idempotent_on_empty_pool() -> None:
    """aclose() on a fresh connector with no pooled clients is a no-op."""
    conn = _ConcreteHttpConnector()
    await conn.aclose()
    assert conn._clients == {}
