# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared Microsoft Entra ID login (client credentials flow).

The login service is mocked with respx. Every id and secret here is made up.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from meho_backplane.connectors._shared.microsoft_login import (
    LOGIN_HOSTS,
    ClientSecretCredentials,
    MicrosoftAccessToken,
    MicrosoftLoginError,
    fetch_client_credentials_token,
)
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from tests._azure_fixtures import (
    ARM_SCOPE,
    CLIENT_A,
    OBJECT_A,
    SECRET_A,
    TENANT_A,
    fake_jwt,
    token_url,
)

_TOKEN_URL = token_url()


async def _fetch(**overrides: Any) -> MicrosoftAccessToken:
    kwargs: dict[str, Any] = {
        "login_host": LOGIN_HOSTS["AzureCloud"],
        "scope": ARM_SCOPE,
        "target_name": "azure-sub-a",
        "secret_ref": "tenants/example/azure-login-a",
    }
    kwargs.update(overrides)
    credentials = ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A)
    return await fetch_client_credentials_token(credentials, **kwargs)


@pytest.mark.asyncio
async def test_sends_the_client_credentials_form_once() -> None:
    token_value = fake_jwt({"appid": CLIENT_A, "oid": OBJECT_A, "tid": TENANT_A})
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_TOKEN_URL).respond(
            200, json={"token_type": "Bearer", "expires_in": 3599, "access_token": token_value}
        )
        token = await _fetch()

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = parse_qs(request.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": [CLIENT_A],
        "client_secret": [SECRET_A],
        "scope": [ARM_SCOPE],
    }
    assert token.value == token_value
    assert token.expires_in == 3599.0


@pytest.mark.asyncio
async def test_reads_the_login_ids_from_a_v1_token() -> None:
    claims = {"ver": "1.0", "appid": CLIENT_A, "oid": OBJECT_A, "tid": TENANT_A}
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(
            200, json={"expires_in": 3599, "access_token": fake_jwt(claims)}
        )
        token = await _fetch()

    assert (token.app_id, token.object_id, token.tenant_id) == (CLIENT_A, OBJECT_A, TENANT_A)


@pytest.mark.asyncio
async def test_reads_the_app_id_from_azp_in_a_v2_token() -> None:
    claims = {"ver": "2.0", "azp": CLIENT_A, "oid": OBJECT_A, "tid": TENANT_A}
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(
            200, json={"expires_in": 3599, "access_token": fake_jwt(claims)}
        )
        token = await _fetch()

    assert token.app_id == CLIENT_A
    assert token.object_id == OBJECT_A


@pytest.mark.asyncio
async def test_a_token_that_is_not_a_jwt_still_works_without_ids() -> None:
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(
            200, json={"expires_in": "3599", "access_token": "opaque-token-value"}
        )
        token = await _fetch()

    assert token.value == "opaque-token-value"
    assert token.expires_in == 3599.0
    assert (token.app_id, token.object_id, token.tenant_id) == (None, None, None)


def test_token_and_secret_are_not_in_repr() -> None:
    token = MicrosoftAccessToken(value="opaque-token-value", expires_in=60.0)
    credentials = ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A)
    assert "opaque-token-value" not in repr(token)
    assert SECRET_A not in repr(credentials)


@pytest.mark.parametrize(
    ("status", "error", "codes"),
    [
        (401, "invalid_client", [7000215]),
        (400, "unauthorized_client", [700016]),
        (400, "invalid_request", [90002]),
    ],
)
@pytest.mark.asyncio
async def test_refused_login_raises_connector_auth_error(
    status: int, error: str, codes: list[int]
) -> None:
    body = {
        "error": error,
        "error_description": f"AADSTS{codes[0]}: free text. Trace ID: x. Correlation ID: y.",
        "error_codes": codes,
    }
    with respx.mock() as mock:
        route = mock.post(_TOKEN_URL).respond(status, json=body)
        with pytest.raises(ConnectorAuthError) as caught:
            await _fetch()

    assert route.call_count == 1
    exc = caught.value
    assert exc.status_code == status
    assert exc.cause == f"session_establish_{status}"
    assert exc.secret_ref == "tenants/example/azure-login-a"
    assert exc.host == "login.microsoftonline.com"
    assert exc.remediation is not None and "client_secret" in exc.remediation
    message = str(exc)
    assert "azure-sub-a" in message
    assert error in message
    assert f"AADSTS{codes[0]}" in message
    assert "Trace ID" not in message
    assert SECRET_A not in message
    assert isinstance(exc.__cause__, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_unknown_error_text_is_left_out_of_the_message() -> None:
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(401, json={"error": "some proxy text", "error_codes": []})
        with pytest.raises(ConnectorAuthError) as caught:
            await _fetch()

    assert "some proxy text" not in str(caught.value)
    assert "HTTP 401)" in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.asyncio
async def test_other_failures_raise_microsoft_login_error_without_retry(status: int) -> None:
    with respx.mock() as mock:
        route = mock.post(_TOKEN_URL).respond(status, json={"error": "temporarily_unavailable"})
        with pytest.raises(MicrosoftLoginError) as caught:
            await _fetch()

    assert route.call_count == 1
    assert str(caught.value) == (
        f"The Microsoft login service at login.microsoftonline.com answered HTTP {status} "
        "for target 'azure-sub-a'. Try again later."
    )
    assert SECRET_A not in str(caught.value)


@pytest.mark.asyncio
async def test_unreachable_login_host_raises_microsoft_login_error() -> None:
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(MicrosoftLoginError) as caught:
            await _fetch()

    assert "could not reach" in str(caught.value)
    assert not isinstance(caught.value, httpx.HTTPError)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expires_in": 3599},
        {"access_token": "", "expires_in": 3599},
        {"access_token": "opaque-token-value"},
        {"access_token": "opaque-token-value", "expires_in": "soon"},
        {"access_token": "opaque-token-value", "expires_in": 0},
        {"access_token": "opaque-token-value", "expires_in": True},
        {"access_token": "opaque-token-value", "expires_in": "inf"},
    ],
)
@pytest.mark.asyncio
async def test_an_answer_without_token_or_lifetime_is_refused(body: dict[str, Any]) -> None:
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(200, json=body)
        with pytest.raises(MicrosoftLoginError, match="no usable access_token"):
            await _fetch()


@pytest.mark.asyncio
async def test_an_answer_that_is_not_json_is_refused() -> None:
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(200, text="<html>proxy page</html>")
        with pytest.raises(MicrosoftLoginError, match="not JSON"):
            await _fetch()


@pytest.mark.asyncio
async def test_uses_the_login_host_it_is_given() -> None:
    gov_url = f"https://login.microsoftonline.us/{TENANT_A}/oauth2/v2.0/token"
    with respx.mock() as mock:
        route = mock.post(gov_url).respond(
            200, json={"expires_in": 3599, "access_token": "opaque-token-value"}
        )
        await _fetch(login_host=LOGIN_HOSTS["AzureUSGovernment"])

    assert route.called


def test_login_hosts_cover_the_three_clouds() -> None:
    assert dict(LOGIN_HOSTS) == {
        "AzureCloud": "login.microsoftonline.com",
        "AzureUSGovernment": "login.microsoftonline.us",
        "AzureChinaCloud": "login.chinacloudapi.cn",
    }
