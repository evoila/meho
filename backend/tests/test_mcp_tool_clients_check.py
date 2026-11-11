# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO lists a per-tool login client only when Keycloak has it.

With ``MCP_CLIENT_PER_TOOL_KIND`` on, ``GET /api/v1/auth-config`` names the
clients ``meho-mcp-code`` and ``meho-mcp-desktop``. These tests check that
MEHO first asks Keycloak (recorded admin answers, no running Keycloak), lists
only what exists, lists nothing when it cannot ask, and says so on
``/ready``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth import tool_clients
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient, KeycloakAdminError
from meho_backplane.auth.tool_clients import (
    MCP_TOOL_CLIENTS,
    ToolClientCheck,
    check_tool_clients,
    listed_tool_clients,
    reset_tool_client_check,
    tool_clients_report,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings

_ADMIN_URL = "https://kc.test/admin/realms/meho"
_TOKEN_URL = "https://kc.test/realms/meho/protocol/openid-connect/token"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", _ADMIN_URL)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-secret")
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "true")
    get_settings.cache_clear()
    reset_tool_client_check()
    yield
    reset_tool_client_check()
    get_settings.cache_clear()


def _mock_keycloak(r: respx.MockRouter, existing: set[str]) -> respx.Route:
    r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok"}))

    def lookup(request: httpx.Request) -> httpx.Response:
        wanted = request.url.params["clientId"]
        found = [{"id": "uuid-" + wanted, "clientId": wanted}] if wanted in existing else []
        return httpx.Response(200, json=found)

    return r.get(f"{_ADMIN_URL}/clients").mock(side_effect=lookup)


def _admin() -> KeycloakAdminClient:
    return KeycloakAdminClient(
        admin_url=_ADMIN_URL, token_url=_TOKEN_URL, client_id="meho-admin", client_secret="x"
    )


# ---------------------------------------------------------------------------
# The admin lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_exists_matches_the_exact_client_id() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
        route = r.get(f"{_ADMIN_URL}/clients").mock(
            side_effect=[
                httpx.Response(200, json=[{"clientId": "meho-mcp-code"}]),
                httpx.Response(200, json=[]),
                # A list that holds another client does not count.
                httpx.Response(200, json=[{"clientId": "meho-mcp-code-old"}]),
            ]
        )
        async with _admin() as kc:
            assert await kc.client_exists("meho-mcp-code") is True
            assert await kc.client_exists("meho-mcp-code") is False
            assert await kc.client_exists("meho-mcp-code") is False
    assert route.calls[0].request.url.params["clientId"] == "meho-mcp-code"
    assert "search" not in route.calls[0].request.url.params


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(403, json={"error": "forbidden"}),
        httpx.Response(500),
        httpx.Response(200, json={"clientId": "meho-mcp-code"}),
        httpx.Response(200, text="not json"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_lookup_raises_and_never_reads_as_missing(reply: httpx.Response) -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
        r.get(f"{_ADMIN_URL}/clients").mock(return_value=reply)
        async with _admin() as kc:
            with pytest.raises(KeycloakAdminError):
                await kc.client_exists("meho-mcp-code")


@pytest.mark.asyncio
async def test_a_network_error_raises() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
        r.get(f"{_ADMIN_URL}/clients").mock(side_effect=httpx.ConnectError("down"))
        async with _admin() as kc:
            with pytest.raises(KeycloakAdminError, match="network error"):
                await kc.client_exists("meho-mcp-code")


# ---------------------------------------------------------------------------
# What auth-config lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_clients_exist_and_both_are_listed() -> None:
    with respx.mock as r:
        _mock_keycloak(r, {"meho-mcp-code", "meho-mcp-desktop"})
        listed = await listed_tool_clients(get_settings())
    assert listed == MCP_TOOL_CLIENTS
    assert tool_clients_report(get_settings()) == {"state": "ok", "missing": [], "detail": None}


@pytest.mark.asyncio
async def test_a_missing_client_is_not_listed_and_ready_names_it() -> None:
    with respx.mock as r:
        _mock_keycloak(r, {"meho-mcp-code"})
        listed = await listed_tool_clients(get_settings())
    assert listed == {"claude-code": "meho-mcp-code"}
    assert tool_clients_report(get_settings()) == {
        "state": "missing",
        "missing": ["meho-mcp-desktop"],
        "detail": None,
    }


@pytest.mark.asyncio
async def test_without_the_admin_client_nothing_is_listed_and_ready_says_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYCLOAK_ADMIN_URL")
    get_settings.cache_clear()
    assert await listed_tool_clients(get_settings()) == {}
    report = tool_clients_report(get_settings())
    assert report is not None
    assert report["state"] == "unknown"
    assert report["missing"] == []
    assert "KEYCLOAK_ADMIN_URL" in report["detail"]


@pytest.mark.asyncio
async def test_when_keycloak_fails_nothing_is_listed() -> None:
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
        r.get(f"{_ADMIN_URL}/clients").mock(return_value=httpx.Response(503))
        assert await listed_tool_clients(get_settings()) == {}
    report = tool_clients_report(get_settings())
    assert report is not None
    assert report["state"] == "unknown"
    assert "HTTP 503" in report["detail"]
    assert "made-up-secret" not in str(report)


@pytest.mark.asyncio
async def test_the_answer_is_kept_so_keycloak_is_not_asked_on_every_call() -> None:
    with respx.mock as r:
        route = _mock_keycloak(r, {"meho-mcp-code", "meho-mcp-desktop"})
        for _ in range(5):
            await listed_tool_clients(get_settings())
    assert route.call_count == len(MCP_TOOL_CLIENTS)


@pytest.mark.asyncio
async def test_an_old_answer_is_checked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    old = ToolClientCheck(
        present=dict(MCP_TOOL_CLIENTS),
        checked_at=time.monotonic() - tool_clients.CHECK_TTL_S - 1,
    )
    monkeypatch.setattr(tool_clients, "_last", old)
    with respx.mock as r:
        _mock_keycloak(r, set())
        check = await check_tool_clients(get_settings())
    assert check.present == {}
    assert check.missing == ("meho-mcp-code", "meho-mcp-desktop")


@pytest.mark.asyncio
async def test_switch_off_asks_keycloak_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "false")
    get_settings.cache_clear()
    with respx.mock as r:
        route = _mock_keycloak(r, {"meho-mcp-code", "meho-mcp-desktop"})
        assert await listed_tool_clients(get_settings()) == {}
        assert tool_clients_report(get_settings()) is None
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_ready_report_does_not_wait_and_starts_a_check() -> None:
    with respx.mock as r:
        _mock_keycloak(r, {"meho-mcp-code"})
        first = tool_clients_report(get_settings())
        assert first == {"state": "unknown", "missing": [], "detail": "not checked yet"}
        assert tool_clients._refresh is not None
        await tool_clients._refresh
    assert tool_clients_report(get_settings()) == {
        "state": "missing",
        "missing": ["meho-mcp-desktop"],
        "detail": None,
    }


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def test_auth_config_route_lists_only_existing_clients() -> None:
    with respx.mock(assert_all_called=False) as r:
        r.route(host="testserver").pass_through()
        _mock_keycloak(r, {"meho-mcp-desktop"})
        response = TestClient(app).get("/api/v1/auth-config")
    assert response.status_code == 200
    assert response.json()["mcp_clients"] == {"claude-desktop": "meho-mcp-desktop"}


def test_ready_shows_the_check_only_with_the_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_clients,
        "_last",
        ToolClientCheck(
            present={"claude-code": "meho-mcp-code"},
            missing=("meho-mcp-desktop",),
            checked_at=time.monotonic(),
        ),
    )
    body = TestClient(app).get("/ready").json()
    assert body["features"]["mcp"]["tool_clients"] == {
        "state": "missing",
        "missing": ["meho-mcp-desktop"],
        "detail": None,
    }

    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "false")
    get_settings.cache_clear()
    body = TestClient(app).get("/ready").json()
    assert "tool_clients" not in body["features"]["mcp"]
