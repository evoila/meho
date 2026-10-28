# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Entra ID connector's plumbing: registration, target, login, calls, pages.

Microsoft's login service and Microsoft Graph are mocked with respx. The
operations themselves are tested through the dispatcher in
``test_connectors_entra_reads.py``. Every id, name and secret here is made up.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from meho_backplane.connectors._shared.microsoft_login import (
    ClientSecretCredentials,
    fetch_client_credentials_token,
)
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.azure.connector import AzureConnector
from meho_backplane.connectors.entra import ENTRA_CONNECTOR_ID, _http, _paging, _paths
from meho_backplane.connectors.entra import connector as entra_connector
from meho_backplane.connectors.entra._credentials import scrub_credentials, summarize
from meho_backplane.connectors.entra._errors import (
    EntraApiError,
    EntraPermissionError,
    EntraThrottledError,
    EntraUrlRefusedError,
)
from meho_backplane.connectors.entra._paging import PageLimits, list_all_pages
from meho_backplane.connectors.entra.connector import EntraConnector
from meho_backplane.connectors.entra.session import (
    EntraSecretShapeError,
    EntraTargetConfigError,
    parse_credentials,
    resolve_target_config,
)
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.operations._lookup import parse_connector_id
from tests._azure_fixtures import (
    CLIENT_A,
    SECRET_A,
    TENANT_A,
    TENANT_B,
    AzureTargetStub,
    CountingLoader,
    fake_jwt,
    operator,
    token_answer,
    token_url,
)
from tests._entra_fixtures import (
    ALL_ROLES,
    CERTIFICATE_DATA,
    ENTRA_SECRET_REF,
    GRAPH_SCOPE,
    ORGANIZATION_URL,
    SECRET_HINT,
    USERS_URL,
    EntraTargetStub,
    app_item,
    entra_loader,
    graph_token,
    next_link,
    organization_answer,
    page,
    user_item,
)


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(entra_connector, "_monotonic", fake)
    return fake


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits of the retry loop instead of sleeping."""
    seen: list[float] = []

    async def fake_sleep(delay: float) -> None:
        seen.append(delay)

    monkeypatch.setattr(_http, "_sleep", fake_sleep)
    return seen


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def _login(mock: respx.MockRouter, token: str | None = None) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(token or graph_token()))


# ----------------------------------------------------------------------
# Registration and paths
# ----------------------------------------------------------------------


def test_registered_with_the_versioned_row_and_the_wildcard_row() -> None:
    registry = all_connectors_v2()

    assert registry[("entra", "1.0", "entra-graph")] is EntraConnector
    assert registry[("entra", "", "")] is EntraConnector
    assert ENTRA_CONNECTOR_ID == "entra-graph-1.0"
    assert parse_connector_id(ENTRA_CONNECTOR_ID) == ("entra", "1.0", "entra-graph")


def _path_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str)
    }


def test_every_graph_path_is_a_v1_path_and_none_is_beta() -> None:
    paths = _path_constants()

    assert len(paths) == 8, sorted(paths)
    for name, value in paths.items():
        assert value.startswith(("/v1.0/", "/beta/")), name
        assert value.startswith("/v1.0/"), f"{name} calls Graph beta: {value}"
        assert "/beta/" not in value, name


def test_every_path_is_exported_and_has_no_query() -> None:
    for name, value in _path_constants().items():
        assert name in _paths.__all__, name
        assert "?" not in value, name


# ----------------------------------------------------------------------
# Target shape and secret shape
# ----------------------------------------------------------------------


def test_a_public_cloud_target_is_accepted() -> None:
    config = resolve_target_config(EntraTargetStub())

    assert config.tenant_id == TENANT_A
    assert config.cloud.name == "AzureCloud"
    assert config.cloud.graph_scope == GRAPH_SCOPE
    assert config.cloud.login_host == "login.microsoftonline.com"


def test_a_government_cloud_target_uses_its_own_graph_host() -> None:
    target = EntraTargetStub(host="graph.microsoft.us", extras={"cloud": "AzureUSGovernment"})

    config = resolve_target_config(target)

    assert config.tenant_id is None
    assert config.cloud.graph_scope == "https://graph.microsoft.us/.default"
    assert config.cloud.login_host == "login.microsoftonline.us"


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"host": "attacker.example.net"}, "host must be 'graph.microsoft.com'"),
        ({"host": "management.azure.com"}, "host must be 'graph.microsoft.com'"),
        ({"port": 8443}, "port 443"),
        ({"extras": {"scheme": "http"}}, "over https"),
        ({"verify_tls": False}, "verify_tls must stay on"),
        ({"extras": {"tenant_id": "not-a-guid"}}, "extras.tenant_id must be"),
        ({"extras": {"cloud": "ExampleCloud"}}, "extras.cloud must be one of"),
    ],
)
def test_a_bad_target_fails_before_any_login(changes: dict[str, Any], words: str) -> None:
    target = EntraTargetStub(**changes)

    with pytest.raises(EntraTargetConfigError) as caught:
        resolve_target_config(target)

    assert words in str(caught.value)
    assert "Entra ID target 'entra-tenant-a'" in str(caught.value)
    assert "not-a-guid" not in str(caught.value)


def test_the_secret_is_stripped_and_its_ids_are_lower_case() -> None:
    credentials = parse_credentials(
        {
            "tenant_id": f" {TENANT_A.upper()}\n",
            "client_id": CLIENT_A.upper(),
            "client_secret": f"{SECRET_A}\n",
            "other": "ignored",
        },
        target_name="t",
        secret_ref="ref",
    )

    assert credentials == ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A)
    assert SECRET_A not in repr(credentials)


@pytest.mark.parametrize(
    ("secret", "words"),
    [
        ({"client_id": CLIENT_A, "client_secret": SECRET_A}, "has no 'tenant_id' key"),
        ({"tenant_id": TENANT_A, "client_id": CLIENT_A, "client_secret": " "}, "'client_secret'"),
        ({"tenant_id": TENANT_A, "client_id": "app", "client_secret": SECRET_A}, "must be a GUID"),
    ],
)
def test_a_bad_secret_names_the_key_and_never_a_value(
    secret: dict[str, object], words: str
) -> None:
    with pytest.raises(EntraSecretShapeError) as caught:
        parse_credentials(secret, target_name="entra-tenant-a", secret_ref="ref")

    assert words in str(caught.value)
    assert SECRET_A not in str(caught.value)


@pytest.mark.asyncio
async def test_a_target_of_another_tenant_than_its_secret_fails_before_the_login() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    target = EntraTargetStub(extras={"tenant_id": TENANT_B})

    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        with pytest.raises(EntraTargetConfigError, match="is not the tenant_id in the secret"):
            await connector.auth_headers(target, operator())

    assert not login.called


# ----------------------------------------------------------------------
# Login and token cache
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_login_asks_for_a_graph_token_and_the_cache_keeps_it(clock: _Clock) -> None:
    loader = entra_loader()
    connector = EntraConnector(credentials_loader=loader)
    target = EntraTargetStub()

    with respx.mock() as mock:
        login = _login(mock)
        first = await connector.auth_headers(target, operator())
        second = await connector.auth_headers(target, operator())

    assert login.call_count == 1
    assert loader.calls == 1
    assert first == second == {"Authorization": f"Bearer {graph_token()}"}
    form = _form(login.calls[0].request)
    assert form["scope"] == [GRAPH_SCOPE]
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [CLIENT_A]


@pytest.mark.asyncio
async def test_a_new_token_is_made_five_minutes_before_the_old_one_runs_out(
    clock: _Clock,
) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    target = EntraTargetStub()

    with respx.mock() as mock:
        login = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(graph_token(n=1), expires_in=3600)),
                httpx.Response(200, json=token_answer(graph_token(n=2), expires_in=3600)),
            ]
        )
        await connector.auth_headers(target, operator())
        clock.now += 3600 - 300 - 1
        still_cached = await connector.auth_headers(target, operator())
        clock.now += 2
        renewed = await connector.auth_headers(target, operator())

    assert login.call_count == 2
    assert still_cached["Authorization"] == f"Bearer {graph_token(n=1)}"
    assert renewed["Authorization"] == f"Bearer {graph_token(n=2)}"


@pytest.mark.asyncio
async def test_the_graph_token_is_kept_apart_from_the_azure_token_of_the_same_login(
    clock: _Clock,
) -> None:
    """One login serves both connectors; each asks for and keeps its own token."""
    credentials = ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A)
    entra = EntraConnector(credentials_loader=CountingLoader({ENTRA_SECRET_REF: credentials}))
    azure = AzureConnector(
        credentials_loader=CountingLoader({"tenants/example/azure-login-a": credentials})
    )

    with respx.mock() as mock:
        login = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(graph_token(n=1))),
                httpx.Response(200, json=token_answer(fake_jwt({"appid": CLIENT_A, "n": 2}))),
            ]
        )
        target = EntraTargetStub()
        graph_headers = await entra.auth_headers(target, operator())
        azure_headers = await azure.auth_headers(AzureTargetStub(), operator())
        graph_again = await entra.auth_headers(target, operator())

    scopes = [_form(call.request)["scope"] for call in login.calls]
    assert scopes == [[GRAPH_SCOPE], ["https://management.azure.com/.default"]]
    assert graph_headers != azure_headers
    assert graph_again == graph_headers


@pytest.mark.asyncio
async def test_a_call_without_a_persons_token_never_uses_the_cache(clock: _Clock) -> None:
    loader = entra_loader()
    connector = EntraConnector(credentials_loader=loader)
    target = EntraTargetStub()

    with respx.mock() as mock:
        login = _login(mock)
        await connector.auth_headers(target, operator())
        await connector.auth_headers(target, synthesise_system_operator())
        await connector.auth_headers(target, operator(raw_jwt=""))

    assert login.call_count == 3
    assert loader.calls == 3


@pytest.mark.asyncio
async def test_invalidate_session_drops_the_token_and_keeps_the_secret(clock: _Clock) -> None:
    loader = entra_loader()
    connector = EntraConnector(credentials_loader=loader)
    target = EntraTargetStub()

    with respx.mock() as mock:
        login = _login(mock)
        await connector.auth_headers(target, operator())
        await connector.invalidate_session(target)
        await connector.auth_headers(target, operator())
        await connector.invalidate_credentials(target)
        await connector.auth_headers(target, operator())

    assert login.call_count == 3
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_a_refused_login_is_a_connector_auth_error_without_the_secret() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        mock.post(token_url()).respond(
            401, json={"error": "invalid_client", "error_codes": [7000215]}
        )
        with pytest.raises(ConnectorAuthError) as caught:
            await connector.auth_headers(EntraTargetStub(), operator())

    assert "AADSTS7000215" in str(caught.value)
    assert SECRET_A not in str(caught.value)


@pytest.mark.asyncio
async def test_the_token_names_its_application_permissions() -> None:
    credentials = ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A)
    tokens = {
        "granted": fake_jwt({"appid": CLIENT_A, "roles": ["User.Read.All", "Group.Read.All", 7]}),
        "none": fake_jwt({"appid": CLIENT_A}),
        "unreadable": "not-a-jwt",
    }
    seen: dict[str, Any] = {}

    with respx.mock() as mock:
        route = mock.post(token_url())
        for name, value in tokens.items():
            route.respond(200, json=token_answer(value))
            token = await fetch_client_credentials_token(
                credentials,
                login_host="login.microsoftonline.com",
                scope=GRAPH_SCOPE,
                target_name="t",
                secret_ref=None,
            )
            seen[name] = token.roles

    assert seen == {
        "granted": ("Group.Read.All", "User.Read.All"),
        "none": (),
        "unreadable": None,
    }


# ----------------------------------------------------------------------
# One Graph call: retries, throttling, 401, 403, redirects
# ----------------------------------------------------------------------


async def _get_users(connector: EntraConnector, target: EntraTargetStub | None = None) -> Any:
    response = await _http.send(
        connector,
        target or EntraTargetStub(),
        _paths.USERS_PATH,
        operator=operator(),
        permission=_paths.USERS_PERMISSION,
    )
    return response.json()


@pytest.mark.asyncio
async def test_a_throttled_call_waits_retry_after_and_tries_again(waits: list[float]) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {}}),
                httpx.Response(200, json=page([user_item(1)])),
            ]
        )
        body = await _get_users(connector)

    assert graph.call_count == 2
    assert waits == [7.0]
    assert body["value"][0]["id"] == user_item(1)["id"]


@pytest.mark.asyncio
async def test_throttling_beyond_the_budget_fails_and_says_when_to_try_again(
    waits: list[float],
) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    throttled = {"error": {"code": "TooManyRequests", "message": "Please retry again later."}}

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(USERS_URL).respond(429, headers={"Retry-After": "30"}, json=throttled)
        with pytest.raises(EntraThrottledError) as caught:
            await _get_users(connector)

    assert graph.call_count == 3
    assert waits == [30.0, 30.0]
    assert caught.value.retry_after_seconds == 30.0
    assert "in 30 seconds" in str(caught.value)
    assert caught.value.upstream_message == "TooManyRequests: Please retry again later."


@pytest.mark.asyncio
async def test_a_server_error_and_a_lost_connection_are_tried_again(waits: list[float]) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(503, json={"error": {"code": "serviceNotAvailable"}}),
                httpx.ConnectError("connection lost"),
                httpx.Response(200, json=page([])),
            ]
        )
        body = await _get_users(connector)

    assert graph.call_count == 3
    assert waits == [0.5, 1.0]
    assert body["value"] == []


@pytest.mark.asyncio
async def test_a_401_logs_in_again_once_and_a_second_401_fails_clearly(
    clock: _Clock,
) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    expired = {"error": {"code": "InvalidAuthenticationToken"}}

    with respx.mock() as mock:
        login = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(graph_token(n=n))) for n in range(1, 5)
            ]
        )
        graph = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(401, json=expired),
                httpx.Response(200, json=page([])),
                httpx.Response(401, json=expired),
                httpx.Response(401, json=expired),
            ]
        )
        await _get_users(connector)
        with pytest.raises(ConnectorAuthError) as caught:
            await _get_users(connector)

    assert login.call_count == 4
    assert graph.call_count == 4
    tokens = [call.request.headers["Authorization"] for call in graph.calls]
    assert tokens[0] != tokens[1]
    assert caught.value.stage == "after_relogin"
    assert "401 again after a new login" in str(caught.value)


@pytest.mark.asyncio
async def test_a_403_names_the_missing_permission_and_the_next_call_gets_a_new_token(
    clock: _Clock,
) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    denied = {
        "error": {
            "code": "Authorization_RequestDenied",
            "message": "Insufficient privileges to complete the operation.",
        }
    }

    with respx.mock() as mock:
        login = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(graph_token(("Group.Read.All",), n=1))),
                httpx.Response(200, json=token_answer(graph_token(ALL_ROLES, n=2))),
            ]
        )
        graph = mock.get(USERS_URL).mock(
            side_effect=[httpx.Response(403, json=denied), httpx.Response(200, json=page([]))]
        )
        with pytest.raises(EntraPermissionError) as caught:
            await _get_users(connector)
        await _get_users(connector)

    message = str(caught.value)
    assert "needs the application permission User.Read.All with admin consent" in message
    assert len(message) <= 256, "the dispatcher keeps only the first 256 characters"
    remediation = caught.value.remediation
    assert remediation.startswith("The login's access token carries: Group.Read.All. ")
    assert "Privileged Role Administrator" in remediation
    assert "Grant admin consent" in remediation
    assert caught.value.http_status == 403
    assert caught.value.graph_error_code == "Authorization_RequestDenied"
    assert login.call_count == 2
    assert graph.calls[1].request.headers["Authorization"] == f"Bearer {graph_token(n=2)}"


@pytest.mark.asyncio
async def test_a_403_with_the_permission_in_the_token_points_elsewhere(clock: _Clock) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        _login(mock, graph_token(("User.Read.All",)))
        mock.get(USERS_URL).respond(403, json={"error": {"code": "Forbidden"}})
        with pytest.raises(EntraPermissionError) as caught:
            await _get_users(connector)

    assert "although the login's token carries User.Read.All" in str(caught.value)
    assert "Conditional Access" in caught.value.remediation


@pytest.mark.asyncio
async def test_a_403_says_when_the_token_carries_no_permission(clock: _Clock) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        _login(mock, graph_token(roles=None))
        mock.get(USERS_URL).respond(403, json={"error": {"code": "Authorization_RequestDenied"}})
        with pytest.raises(EntraPermissionError) as caught:
            await _get_users(connector)

    assert "carries no Microsoft Graph application permission" in caught.value.remediation


@pytest.mark.asyncio
async def test_other_errors_are_not_retried_and_keep_graphs_code(waits: list[float]) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    bad = {"error": {"code": "Request_UnsupportedQuery", "message": "Unsupported query."}}

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(USERS_URL).respond(400, json=bad)
        with pytest.raises(EntraApiError) as caught:
            await _get_users(connector)

    assert graph.call_count == 1
    assert waits == []
    assert caught.value.http_status == 400
    assert caught.value.upstream_message == "Request_UnsupportedQuery: Unsupported query."


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(USERS_URL).respond(302, headers={"Location": "https://attacker.example.net/x"})
        attacker = mock.get("https://attacker.example.net/x").respond(200, json={})
        with pytest.raises(EntraUrlRefusedError, match="does not follow redirects"):
            await _get_users(connector)

    assert not attacker.called


# ----------------------------------------------------------------------
# Next-page links and paging
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "words"),
    [
        ("https://attacker.example.net/v1.0/users?$skiptoken=x", "another host"),
        ("http://graph.microsoft.com/v1.0/users?$skiptoken=x", "plain https"),
        ("https://graph.microsoft.com:8443/v1.0/users?$skiptoken=x", "plain https"),
        ("https://someone@graph.microsoft.com/v1.0/users?$skiptoken=x", "plain https"),
        ("https://graph.microsoft.com/v1.0/applications?$skiptoken=x", "same list"),
        ("https://graph.microsoft.com/beta/users?$skiptoken=x", "same list"),
        ("https://graph.microsoft.com/v1.0/users/../applications", "same list"),
        ("https://graph.microsoft.com/v1.0/users/%2e%2e/applications", "same list"),
        ("/v1.0/users?$skiptoken=x", "plain https"),
        ("https://graph.microsoft.com/v1.0/users?$skiptoken=" + "x" * 9000, "longer than"),
    ],
)
def test_a_next_page_link_that_leaves_the_list_is_refused(link: str, words: str) -> None:
    with pytest.raises(EntraUrlRefusedError) as caught:
        _http.check_next_link(EntraTargetStub(), link, path=_paths.USERS_PATH)

    assert words in str(caught.value)
    assert "attacker" not in str(caught.value)


def test_a_next_page_link_of_the_same_list_is_accepted() -> None:
    link = "https://graph.microsoft.com/v1.0/Users?$top=999&$skiptoken=abc"

    assert _http.check_next_link(EntraTargetStub(), link, path=_paths.USERS_PATH) == link


@pytest.mark.asyncio
async def test_paging_follows_next_links_until_the_end() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(200, json=page([user_item(1)], next_link(USERS_URL, "p2"))),
                httpx.Response(200, json=page([user_item(2)], next_link(USERS_URL, "p3"))),
                httpx.Response(200, json=page([user_item(3)])),
            ]
        )
        paged = await list_all_pages(
            connector,
            EntraTargetStub(),
            _paths.USERS_PATH,
            operator=operator(),
            permission=_paths.USERS_PERMISSION,
            params={"$top": "999"},
        )

    assert [row["id"] for row in paged.rows] == [user_item(n)["id"] for n in (1, 2, 3)]
    assert paged.as_dict()["truncated"] is False
    assert paged.as_dict()["pages_read"] == 3
    assert graph.calls[0].request.url.params["$top"] == "999"
    assert graph.calls[2].request.url.params["$skiptoken"] == "p3"


@pytest.mark.asyncio
async def test_paging_refuses_a_link_to_another_host_before_any_request() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    bad = page([user_item(1)], "https://attacker.example.net/v1.0/users?$skiptoken=x")

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(USERS_URL).respond(200, json=bad)
        attacker = mock.get("https://attacker.example.net/v1.0/users").respond(200, json={})
        with pytest.raises(EntraUrlRefusedError):
            await list_all_pages(
                connector,
                EntraTargetStub(),
                _paths.USERS_PATH,
                operator=operator(),
                permission=_paths.USERS_PERMISSION,
            )

    assert not attacker.called


@pytest.mark.asyncio
async def test_paging_stops_at_its_limits_and_says_what_was_left_out() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    endless = page([user_item(1), user_item(2)], next_link(USERS_URL))

    with respx.mock() as mock:
        _login(mock)
        mock.get(USERS_URL).respond(200, json=endless)
        by_pages = await list_all_pages(
            connector,
            EntraTargetStub(),
            _paths.USERS_PATH,
            operator=operator(),
            permission=_paths.USERS_PERMISSION,
            limits=PageLimits(max_pages=2),
        )
        by_rows = await list_all_pages(
            connector,
            EntraTargetStub(),
            _paths.USERS_PATH,
            operator=operator(),
            permission=_paths.USERS_PERMISSION,
            limits=PageLimits(max_rows=3),
        )

    pages = by_pages.as_dict()
    assert (pages["truncated"], pages["omission_kind"], pages["pages_read"]) == (
        True,
        "page_limit",
        2,
    )
    assert "Microsoft Graph has more rows" in pages["omission"]
    rows = by_rows.as_dict()
    assert (rows["omission_kind"], len(rows["value"])) == ("row_limit", 3)


@pytest.mark.asyncio
async def test_paging_stops_at_its_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    ticks = iter([0.0, 500.0])
    monkeypatch.setattr(_paging, "_monotonic", ticks.__next__)

    with respx.mock() as mock:
        _login(mock)
        mock.get(USERS_URL).respond(200, json=page([user_item(1)], next_link(USERS_URL)))
        paged = await list_all_pages(
            connector,
            EntraTargetStub(),
            _paths.USERS_PATH,
            operator=operator(),
            permission=_paths.USERS_PERMISSION,
        )

    assert paged.as_dict()["omission_kind"] == "time_limit"


# ----------------------------------------------------------------------
# Credentials never reach a trace
# ----------------------------------------------------------------------


def test_scrub_credentials_keeps_only_ids_names_and_dates() -> None:
    answer = page([app_item(1, secret_ends=("2027-01-01T00:00:00Z",), certificate_ends=(None,))])

    cleaned = scrub_credentials(answer)

    text = json.dumps(cleaned)
    assert SECRET_HINT not in text
    assert CERTIFICATE_DATA not in text
    assert "secretText" not in text
    assert "customKeyIdentifier" not in text
    secret = cleaned["value"][0]["passwordCredentials"][0]
    assert set(secret) == {"keyId", "displayName", "startDateTime", "endDateTime"}
    assert cleaned["value"][0]["displayName"] == "Example App 1"


def test_the_trace_hook_records_app_answers_without_hints() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())
    answer = page([app_item(1, secret_ends=("2027-01-01T00:00:00Z",))])

    recorded = connector.trace_response_body(httpx.Response(200, json=answer))
    plain = connector.trace_response_body(httpx.Response(200, text="42 is not json {"))

    assert SECRET_HINT not in json.dumps(recorded)
    assert plain is None


@pytest.mark.asyncio
async def test_a_traced_call_records_the_app_answer_without_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a trace is captured, the recorded body is the filtered one."""
    recorded: list[Any] = []
    monkeypatch.setattr(_http.flight_recorder_capture, "span_start", object)

    def capture(_span: Any, **kwargs: Any) -> None:
        recorded.append(kwargs["response_body"])

    monkeypatch.setattr(_http.flight_recorder_capture, "record_vendor_call", capture)
    connector = EntraConnector(credentials_loader=entra_loader())
    answer = page([app_item(1, secret_ends=("2027-01-01T00:00:00Z",), certificate_ends=(None,))])

    with respx.mock() as mock:
        _login(mock)
        mock.get("https://graph.microsoft.com/v1.0/applications").respond(200, json=answer)
        await _http.send(
            connector,
            EntraTargetStub(),
            _paths.APPLICATIONS_PATH,
            operator=operator(),
            permission=_paths.APPLICATIONS_PERMISSION,
        )

    assert len(recorded) == 1
    text = json.dumps(recorded[0])
    assert "Example App 1" in text
    assert SECRET_HINT not in text
    assert CERTIFICATE_DATA not in text


def test_summarize_counts_expired_credentials_and_finds_the_next_end() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    credentials = [
        {"endDateTime": "2026-10-01T00:00:00Z"},
        {"endDateTime": "2026-12-01T00:00:00.1234567Z"},
        {"endDateTime": "2026-11-15T00:00:00Z"},
        {"endDateTime": None},
        {"endDateTime": "not a date"},
        "not a credential",
    ]

    summary = summarize(credentials, now)

    assert summary.count == 5
    assert summary.expired == 1
    assert summary.next_end == datetime(2026, 11, 15, tzinfo=UTC)
    assert summarize(None, now).count == 0


# ----------------------------------------------------------------------
# Fingerprint and probe
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_describes_the_tenant() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    with respx.mock() as mock:
        _login(mock)
        graph = mock.get(ORGANIZATION_URL).respond(200, json=organization_answer())
        fp = await connector.fingerprint(EntraTargetStub(), operator=operator())

    assert fp.reachable is True
    assert (fp.vendor, fp.product, fp.version) == ("microsoft", "entra", None)
    assert dict(fp.extras) == {
        "tenant_id": TENANT_A,
        "cloud": "AzureCloud",
        "default_domain": "example.com",
    }
    assert graph.calls[0].request.url.params["$select"] == "id,displayName,verifiedDomains"


@pytest.mark.asyncio
async def test_fingerprint_without_an_operator_reports_why_it_cannot_read() -> None:
    from meho_backplane.auth.vault import VaultClientError

    async def vault_refuses(_target: Any, _operator: Any) -> Any:
        raise VaultClientError("Vault refused the login of the system identity")

    connector = EntraConnector(credentials_loader=vault_refuses)

    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        fp = await connector.fingerprint(EntraTargetStub())

    assert fp.reachable is False
    assert "VaultClientError" in fp.extras["error"]
    assert not login.called


@pytest.mark.asyncio
async def test_probe_uses_the_fingerprint_read() -> None:
    connector = EntraConnector(credentials_loader=entra_loader())

    probe = await connector.probe(EntraTargetStub(host="attacker.example.net"))

    assert probe.ok is False
    assert probe.reason is not None and "host must be" in probe.reason
