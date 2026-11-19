# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#6099 — a profile client made by ``bootstrap-clients`` ties each token to its profile.

``meho admin keycloak bootstrap-clients --mcp-profile investigate`` makes
the client ``meho-mcp-profile-investigate``. A fixed claim mapper on the
client writes ``mcp_profile: investigate`` into every token. This test
proves, against a real Keycloak:

- the client has the fixed claim mapper;
- a token from a browser login carries the claim, also a renewed token,
  a token renewed with fewer scopes, and a login that asks only for
  ``openid``: the session cannot get a token without the claim;
- MEHO's token check refuses such a token at the REST audience (403) and
  takes it only where the MCP route allows it;
- a ``meho-mcp`` token has no such claim.

Skips like :mod:`tests.integration.test_keycloak_bootstrap_mcp_login`:
when ``MEHO_TEST_KEYCLOAK_IMAGE`` is unset, when Docker is missing, or
when the Go toolchain is missing (CI provides all three).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt_for_audience
from meho_backplane.settings import get_settings
from tests.integration.test_keycloak_bootstrap_mcp_login import (
    _BACKPLANE_AUDIENCE,
    _HTTP_TIMEOUT,
    _MCP_RESOURCE_URI,
    _REALM,
    _browser_login_tokens,
    _client_mappers,
    _plugin_default_scopes,
    _run_bootstrap,
    fresh_keycloak,  # noqa: F401 — pytest-discovered fixture
    meho_binary,  # noqa: F401 — pytest-discovered fixture
)

_PROFILE = "investigate"
_CLIENT = f"meho-mcp-profile-{_PROFILE}"


@pytest.fixture(scope="module")
def profile_realm(fresh_keycloak: str, meho_binary: Path) -> str:  # noqa: F811
    """The bootstrapped realm, run again with ``--mcp-profile``."""
    _run_bootstrap(meho_binary, fresh_keycloak, _REALM, "--mcp-profile", _PROFILE)
    return fresh_keycloak


def _claims(access_token: str) -> dict[str, Any]:
    """The payload of *access_token*, not checked (Keycloak made it here)."""
    payload = access_token.split(".")[1]
    return dict(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))


def _renew(base_url: str, refresh_token: str, scope: str | None = None) -> dict[str, Any]:
    data = {"grant_type": "refresh_token", "client_id": _CLIENT, "refresh_token": refresh_token}
    if scope is not None:
        data["scope"] = scope
    resp = httpx.post(
        f"{base_url}/realms/{_REALM}/protocol/openid-connect/token",
        data=data,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return dict(resp.json())


async def _check(
    base_url: str, access_token: str, monkeypatch: pytest.MonkeyPatch, *, mcp: bool
) -> Any:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"{base_url}/realms/{_REALM}")
    get_settings.cache_clear()
    clear_jwks_cache()
    if mcp:
        return await verify_jwt_for_audience(
            f"Bearer {access_token}", expected_audience=_MCP_RESOURCE_URI, allow_mcp_profile=True
        )
    return await verify_jwt_for_audience(
        f"Bearer {access_token}", expected_audience=_BACKPLANE_AUDIENCE
    )


def test_profile_client_has_the_fixed_claim_mapper(profile_realm: str) -> None:
    mapper = _client_mappers(profile_realm, _REALM, _CLIENT)["mcp-profile"]
    assert mapper["protocolMapper"] == "oidc-hardcoded-claim-mapper"
    assert mapper["config"]["claim.name"] == "mcp_profile"
    assert mapper["config"]["claim.value"] == _PROFILE
    assert "mcp-profile" not in _client_mappers(profile_realm, _REALM, "meho-mcp")


def test_every_token_of_the_profile_client_carries_the_claim(profile_realm: str) -> None:
    login = _browser_login_tokens(profile_realm, _plugin_default_scopes(), client_id=_CLIENT)
    renewed = _renew(profile_realm, login["refresh_token"])
    fewer = _renew(profile_realm, renewed["refresh_token"], scope="mcp:read")
    minimal = _browser_login_tokens(profile_realm, "openid", client_id=_CLIENT)
    for name, answer in {
        "login": login,
        "renewed": renewed,
        "fewer scopes": fewer,
        "openid only": minimal,
    }.items():
        assert _claims(answer["access_token"]).get("mcp_profile") == _PROFILE, name


@pytest.mark.asyncio
async def test_meho_takes_the_token_only_where_a_profile_is_allowed(
    profile_realm: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _browser_login_tokens(profile_realm, _plugin_default_scopes(), client_id=_CLIENT)
    with pytest.raises(HTTPException) as caught:
        await _check(profile_realm, token["access_token"], monkeypatch, mcp=False)
    assert caught.value.status_code == 403
    assert caught.value.detail == "mcp_profile_scope_violation"
    operator = await _check(profile_realm, token["access_token"], monkeypatch, mcp=True)
    assert operator.mcp_profile_claim == _PROFILE


def test_a_meho_mcp_token_has_no_profile_claim(profile_realm: str) -> None:
    login = _browser_login_tokens(profile_realm, _plugin_default_scopes())
    assert "mcp_profile" not in _claims(login["access_token"])
