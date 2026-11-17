# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A login token tied to one MCP tool profile (#6099).

A login client made for one profile writes ``mcp_profile: <name>`` into
every token. These tests run the real token check (signed tokens, a mocked
key set) and check:

* the token works at ``/mcp?profile=<that name>``;
* it gets 403 (not 401) at ``/mcp``, at another profile, at a REST route
  and at the status route;
* a token without the claim works as before, with or without ``?profile=``;
* a claim value MEHO cannot read, or a profile MEHO does not know, is
  refused everywhere;
* the check is in the one function every entrance uses, so an entrance
  that does not allow it on purpose refuses the token.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.auth.mcp_profile_claim import read_mcp_profile_claim
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE, ISSUER, make_rsa_keypair, mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"
_MCP_URI = f"{_BACKPLANE_URL}/mcp"
_PROFILES = {"k8s": {"tools": "investigate", "connectors": ["kubernetes"]}}
_PING: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true")
    monkeypatch.setenv("MCP_CONNECTOR_PROFILES", json.dumps(_PROFILES))
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def key() -> Any:
    return make_rsa_keypair(kid="mcp-profile-token-kid")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _token(key: Any, **claims: Any) -> str:
    """A token of a profile client: both audiences, like the MCP clients."""
    return mint_token(key, extra_claims={"aud": [AUDIENCE, _MCP_URI], **claims})


def _send(client: TestClient, key: Any, method: str, url: str, **claims: Any) -> Any:
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        token = _token(key, **claims)
        headers = {"Authorization": f"Bearer {token}"}
        if method == "POST":
            return client.post(url, json=_PING, headers=headers)
        return client.get(url, headers=headers)


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "url"), [("k8s", "k8s"), ("K8S", "k8s"), ("investigate", "investigate")]
)
def test_profile_token_works_at_its_profile_url(
    client: TestClient, key: Any, claim: str, url: str
) -> None:
    response = _send(client, key, "POST", f"/mcp?profile={url}", mcp_profile=claim)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {}


@pytest.mark.parametrize("url", ["/mcp", "/mcp?profile=investigate", "/mcp?profile="])
def test_profile_token_is_refused_at_another_mcp_url(
    client: TestClient, key: Any, url: str
) -> None:
    response = _send(client, key, "POST", url, mcp_profile="k8s")
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert "/mcp?profile=k8s" in response.json()["error"]["message"]


@pytest.mark.parametrize("path", ["/api/v1/health/live", "/api/v1/health", "/api/v1/connectors"])
def test_profile_token_is_refused_at_rest_and_status(
    client: TestClient, key: Any, path: str
) -> None:
    response = _send(client, key, "GET", path, mcp_profile="k8s")
    assert response.status_code == 403
    assert response.json()["detail"] == "mcp_profile_scope_violation"


@pytest.mark.parametrize("url", ["/mcp", "/mcp?profile=k8s", "/mcp?profile=investigate"])
def test_token_without_the_claim_works_as_before(client: TestClient, key: Any, url: str) -> None:
    response = _send(client, key, "POST", url)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        (None, "malformed_mcp_profile_claim"),
        (123, "malformed_mcp_profile_claim"),
        ("", "malformed_mcp_profile_claim"),
        (["k8s"], "malformed_mcp_profile_claim"),
        ("nope", "unknown_mcp_profile_claim"),
    ],
)
@pytest.mark.parametrize(
    ("method", "url"),
    [("POST", "/mcp?profile=k8s"), ("POST", "/mcp"), ("GET", "/api/v1/health/live")],
)
def test_bad_or_unknown_claim_is_refused_everywhere(
    client: TestClient, key: Any, value: Any, detail: str, method: str, url: str
) -> None:
    response = _send(client, key, method, url, mcp_profile=value)
    assert response.status_code == 403
    assert response.json()["detail"] == detail


@pytest.mark.parametrize("url", ["/mcp?profile=k8s", "/mcp"])
def test_profile_unknown_once_the_switch_is_off(
    client: TestClient, key: Any, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "false")
    get_settings.cache_clear()
    response = _send(client, key, "POST", url, mcp_profile="k8s")
    assert response.status_code == 403
    assert response.json()["detail"] == "unknown_mcp_profile_claim"


# ---------------------------------------------------------------------------
# The one function every entrance uses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_entrance_refuses_a_profile_token_unless_it_allows_it(key: Any) -> None:
    with respx.mock as router:
        _mock_discovery_and_jwks(router, _public_jwks(key))
        token = _token(key, mcp_profile="k8s")
        with pytest.raises(HTTPException) as caught:
            await verify_jwt_for_audience(f"Bearer {token}", expected_audience=AUDIENCE)
        allowed = await verify_jwt_for_audience(
            f"Bearer {token}", expected_audience=_MCP_URI, allow_mcp_profile=True
        )
    assert caught.value.status_code == 403
    assert caught.value.detail == "mcp_profile_scope_violation"
    assert allowed.mcp_profile_claim == "k8s"
    assert allowed.mcp_tool_profile is None


@pytest.mark.parametrize(
    ("claims", "expected"),
    [({}, None), ({"mcp_profile": " K8s "}, "k8s")],
)
def test_the_claim_is_read(claims: dict[str, Any], expected: str | None) -> None:
    assert read_mcp_profile_claim(claims) == expected


def test_a_null_claim_is_refused() -> None:
    with pytest.raises(HTTPException) as caught:
        read_mcp_profile_claim({"mcp_profile": None})
    assert caught.value.status_code == 403
    assert caught.value.detail == "malformed_mcp_profile_claim"
