# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure connector's login, token cache and recovery hooks.

The Microsoft login service is mocked with respx; the secret comes from a
stub loader. Every id, name and secret here is made up.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
import structlog

from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.azure import AZURE_CONNECTOR_ID
from meho_backplane.connectors.azure import connector as azure_connector
from meho_backplane.connectors.azure.connector import AzureConnector
from meho_backplane.connectors.azure.session import AzureCredentials, AzureTargetConfigError
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.operations._lookup import parse_connector_id
from tests._azure_fixtures import (
    CLIENT_A,
    CLIENT_B,
    OBJECT_B,
    SECRET_A,
    SECRET_B,
    SUBSCRIPTION_A,
    SUBSCRIPTION_B,
    TENANT_A,
    TENANT_B,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

_REF_B = "tenants/example/azure-login-b"


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(azure_connector, "_monotonic", fake)
    return fake


def _bearer(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def test_registered_with_the_versioned_row_and_the_wildcard_row() -> None:
    registry = all_connectors_v2()

    assert registry[("azure", "2026.10", "azure-arm")] is AzureConnector
    assert registry[("azure", "", "")] is AzureConnector
    assert AZURE_CONNECTOR_ID == "azure-arm-2026.10"
    assert parse_connector_id(AZURE_CONNECTOR_ID) == ("azure", "2026.10", "azure-arm")


@pytest.mark.asyncio
async def test_first_call_logs_in_and_the_next_one_uses_the_cache(clock: _Clock) -> None:
    loader = CountingLoader()
    connector = AzureConnector(credentials_loader=loader)
    target = AzureTargetStub()

    with respx.mock() as mock:
        route = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        first = await connector.auth_headers(target, operator())
        second = await connector.auth_headers(target, operator())

    assert route.call_count == 1
    assert loader.calls == 1
    assert first == second == {"Authorization": f"Bearer {login_token()}"}
    assert _form(route.calls[0].request)["scope"] == ["https://management.azure.com/.default"]


@pytest.mark.asyncio
async def test_two_targets_with_two_secrets_get_two_tokens(clock: _Clock) -> None:
    loader = CountingLoader(
        {
            "tenants/example/azure-login-a": AzureCredentials(TENANT_A, CLIENT_A, SECRET_A),
            _REF_B: AzureCredentials(TENANT_B, CLIENT_B, SECRET_B),
        }
    )
    connector = AzureConnector(credentials_loader=loader)
    target_a = AzureTargetStub()
    target_b = AzureTargetStub(
        name="azure-sub-b", secret_ref=_REF_B, extras={"subscription_id": SUBSCRIPTION_B}
    )

    with respx.mock() as mock:
        route_a = mock.post(token_url(TENANT_A)).respond(200, json=token_answer(login_token()))
        route_b = mock.post(token_url(TENANT_B)).respond(
            200, json=token_answer(login_token(CLIENT_B, OBJECT_B))
        )
        headers_a = await connector.auth_headers(target_a, operator())
        headers_b = await connector.auth_headers(target_b, operator())

    assert route_a.call_count == route_b.call_count == 1
    assert _form(route_a.calls[0].request)["client_secret"] == [SECRET_A]
    assert _form(route_b.calls[0].request)["client_secret"] == [SECRET_B]
    assert _bearer(headers_a) != _bearer(headers_b)


@pytest.mark.asyncio
async def test_two_targets_sharing_one_secret_keep_their_own_tokens(clock: _Clock) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target_a = AzureTargetStub()
    target_b = AzureTargetStub(name="azure-sub-b", extras={"subscription_id": SUBSCRIPTION_B})

    with respx.mock() as mock:
        route = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(login_token(n=1))),
                httpx.Response(200, json=token_answer(login_token(n=2))),
            ]
        )
        headers_a = await connector.auth_headers(target_a, operator())
        headers_b = await connector.auth_headers(target_b, operator())
        await connector.invalidate_session(target_a)
        headers_b_again = await connector.auth_headers(target_b, operator())

    assert route.call_count == 2
    assert _bearer(headers_a) != _bearer(headers_b)
    assert headers_b_again == headers_b


@pytest.mark.asyncio
async def test_the_token_is_renewed_five_minutes_before_it_runs_out(clock: _Clock) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub()

    with respx.mock() as mock:
        route = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(login_token(n=1), expires_in=3600)),
                httpx.Response(200, json=token_answer(login_token(n=2), expires_in=3600)),
            ]
        )
        first = await connector.auth_headers(target, operator())
        clock.now += 3600 - 300 - 1
        still_cached = await connector.auth_headers(target, operator())
        clock.now += 2
        renewed = await connector.auth_headers(target, operator())

    assert route.call_count == 2
    assert still_cached == first
    assert _bearer(renewed) == login_token(n=2)


@pytest.mark.asyncio
async def test_ten_parallel_calls_at_expiry_start_one_login(clock: _Clock) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub()
    logins = 0

    async def slow_login(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        logins += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=token_answer(login_token(n=logins), expires_in=3600))

    with respx.mock() as mock:
        mock.post(token_url()).mock(side_effect=slow_login)
        await connector.auth_headers(target, operator())
        clock.now += 3600
        results = await asyncio.gather(
            *(connector.auth_headers(target, operator()) for _ in range(10))
        )

    assert logins == 2
    assert {_bearer(headers) for headers in results} == {login_token(n=2)}


@pytest.mark.asyncio
async def test_invalidate_session_drops_the_token_and_keeps_the_secret(clock: _Clock) -> None:
    loader = CountingLoader()
    connector = AzureConnector(credentials_loader=loader)
    target = AzureTargetStub()

    with respx.mock() as mock:
        route = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(login_token(n=1))),
                httpx.Response(200, json=token_answer(login_token(n=2))),
            ]
        )
        await connector.auth_headers(target, operator())
        await connector.invalidate_session(target)
        after = await connector.auth_headers(target, operator())

    assert route.call_count == 2
    assert loader.calls == 1
    assert _bearer(after) == login_token(n=2)


@pytest.mark.asyncio
async def test_invalidate_credentials_reads_the_secret_again(clock: _Clock) -> None:
    loader = CountingLoader()
    connector = AzureConnector(credentials_loader=loader)
    target = AzureTargetStub()

    with respx.mock() as mock:
        route = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        await connector.auth_headers(target, operator())
        loader.logins[target.secret_ref] = AzureCredentials(TENANT_A, CLIENT_A, SECRET_B)
        await connector.invalidate_credentials(target)
        await connector.auth_headers(target, operator())

    assert loader.calls == 2
    assert route.call_count == 2
    assert _form(route.calls[1].request)["client_secret"] == [SECRET_B]


@pytest.mark.asyncio
async def test_a_refused_login_raises_and_caches_nothing(clock: _Clock) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub()
    refusal = {"error": "invalid_client", "error_codes": [7000215]}

    with respx.mock() as mock:
        route = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(401, json=refusal),
                httpx.Response(200, json=token_answer(login_token())),
            ]
        )
        with pytest.raises(ConnectorAuthError) as caught:
            await connector.auth_headers(target, operator())
        headers = await connector.auth_headers(target, operator())

    assert route.call_count == 2
    assert caught.value.status_code == 401
    assert SECRET_A not in str(caught.value)
    assert _bearer(headers) == login_token()


@pytest.mark.asyncio
async def test_a_tenant_mismatch_fails_before_any_login(clock: _Clock) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub(extras={"subscription_id": SUBSCRIPTION_A, "tenant_id": TENANT_B})

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        with pytest.raises(AzureTargetConfigError, match=r"extras\.tenant_id"):
            await connector.auth_headers(target, operator())

    assert not route.called


@pytest.mark.asyncio
async def test_a_target_on_another_host_never_gets_a_token(clock: _Clock) -> None:
    loader = CountingLoader()
    connector = AzureConnector(credentials_loader=loader)
    target = AzureTargetStub(host="attacker.example.net")

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        with pytest.raises(AzureTargetConfigError, match="host must be"):
            await connector.auth_headers(target, operator())

    assert not route.called
    assert loader.calls == 0


@pytest.mark.parametrize("caller", ["system", "empty-jwt"])
@pytest.mark.asyncio
async def test_a_call_without_a_persons_token_never_gets_a_cached_token(
    clock: _Clock, caller: str
) -> None:
    loader = CountingLoader()
    connector = AzureConnector(credentials_loader=loader)
    target = AzureTargetStub()
    other = synthesise_system_operator() if caller == "system" else operator(raw_jwt="")

    with respx.mock() as mock:
        route = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        await connector.auth_headers(target, operator())
        await connector.auth_headers(target, other)

    assert loader.calls == 2
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_an_unsupported_auth_model_is_refused() -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub(auth_model="per_operator_impersonation")

    with pytest.raises(NotImplementedError, match="auth_model"):
        await connector.auth_headers(target, operator())


@pytest.mark.asyncio
async def test_aclose_forgets_cached_secrets_and_tokens(clock: _Clock) -> None:
    loader = CountingLoader()
    connector = AzureConnector(credentials_loader=loader)
    target = AzureTargetStub()

    with respx.mock() as mock:
        route = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        await connector.auth_headers(target, operator())
        await connector.aclose()
        await connector.auth_headers(target, operator())

    assert loader.calls == 2
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_no_token_or_secret_reaches_a_log_line(
    clock: _Clock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # structlog caches a logger on its first use, with the processors of that
    # moment. When an earlier test used it and then changed the structlog
    # setup, capture_logs below would miss it. A fresh logger is always seen.
    monkeypatch.setattr(azure_connector, "_log", structlog.get_logger(azure_connector.__name__))
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub()
    refusal = {"error": "invalid_client", "error_codes": [7000215]}

    with structlog.testing.capture_logs() as events, respx.mock() as mock:
        mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(login_token())),
                httpx.Response(401, json=refusal),
            ]
        )
        await connector.auth_headers(target, operator())
        await connector.invalidate_credentials(target)
        with pytest.raises(ConnectorAuthError):
            await connector.auth_headers(target, operator())

    logged: list[Any] = [*events, *(record.getMessage() for record in caplog.records)]
    text = repr(logged)
    assert "azure_login_done" in text
    assert login_token() not in text
    assert SECRET_A not in text
