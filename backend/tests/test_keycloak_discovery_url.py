# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``KEYCLOAK_DISCOVERY_URL``: the backplane's own calls to Keycloak (#4591).

With the built-in Keycloak, pods reach Keycloak through its Service while
tokens carry the public issuer. ``KEYCLOAK_DISCOVERY_URL`` is the realm
base URL for the discovery document and the token endpoint; the issuer
check keeps ``KEYCLOAK_ISSUER_URL``. Empty or unset, every call goes to the
issuer as before.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi import HTTPException

from meho_backplane.auth.jwt import (
    clear_jwks_cache,
    keycloak_readiness_probe,
    verify_jwt_for_audience,
)
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.settings import Settings, get_settings
from meho_backplane.ui.auth.flow import _fetch_discovery_doc
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    DISCOVERY_URL,
    ISSUER,
    JWKS_URL,
    make_rsa_keypair,
    mint_token,
    public_jwks,
)

_INTERNAL = "http://test-keycloak:8080/auth/realms/meho"
_INTERNAL_DISCOVERY = f"{_INTERNAL}/.well-known/openid-configuration"
_INTERNAL_JWKS = f"{_INTERNAL}/protocol/openid-connect/certs"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("KEYCLOAK_DISCOVERY_URL", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _use_discovery_url(monkeypatch: pytest.MonkeyPatch, value: str) -> Settings:
    monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", value)
    get_settings.cache_clear()
    return get_settings()


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_means_the_issuer(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", value)
    settings = get_settings()
    assert settings.keycloak_discovery_url is None
    assert settings.keycloak_discovery_base == ISSUER


def test_set_value_is_the_base_without_a_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _use_discovery_url(monkeypatch, _INTERNAL + "/")
    assert settings.keycloak_discovery_base == _INTERNAL
    assert str(settings.keycloak_issuer_url).rstrip("/") == ISSUER


def test_a_value_that_is_not_a_url_fails_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", "not a url")
    with pytest.raises(ValueError, match="keycloak_discovery_url"):
        get_settings()


async def _verify(token: str) -> str:
    operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience=AUDIENCE)
    return operator.sub


async def test_empty_value_fetches_keys_through_the_issuer_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", "")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-A")
    with respx.mock as router:
        discovery = router.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        )
        router.get(JWKS_URL).mock(return_value=httpx.Response(200, json=public_jwks(key)))
        assert await _verify(mint_token(key)) == "op-42"
    assert discovery.call_count == 1


async def test_set_value_fetches_keys_in_cluster_and_keeps_the_public_issuer_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_discovery_url(monkeypatch, _INTERNAL)
    key = make_rsa_keypair("kid-A")
    # What Keycloak answers at the Service address with a fixed public
    # hostname and the dynamic back channel: public issuer, internal keys.
    with respx.mock(assert_all_called=True) as router:
        router.get(_INTERNAL_DISCOVERY).mock(
            return_value=httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": _INTERNAL_JWKS})
        )
        router.get(_INTERNAL_JWKS).mock(return_value=httpx.Response(200, json=public_jwks(key)))
        assert await _verify(mint_token(key)) == "op-42"


async def test_set_value_still_refuses_a_token_with_the_internal_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_discovery_url(monkeypatch, _INTERNAL)
    key = make_rsa_keypair("kid-A")
    token = mint_token(key, extra_claims={"iss": _INTERNAL})
    with respx.mock as router:
        router.get(_INTERNAL_DISCOVERY).mock(
            return_value=httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": _INTERNAL_JWKS})
        )
        router.get(_INTERNAL_JWKS).mock(return_value=httpx.Response(200, json=public_jwks(key)))
        with pytest.raises(HTTPException) as excinfo:
            await _verify(token)
    assert excinfo.value.status_code == 401


async def test_readiness_probe_uses_the_discovery_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_discovery_url(monkeypatch, _INTERNAL)
    key = make_rsa_keypair("kid-A")
    with respx.mock(assert_all_called=True) as router:
        router.get(_INTERNAL_DISCOVERY).mock(
            return_value=httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": _INTERNAL_JWKS})
        )
        router.get(_INTERNAL_JWKS).mock(return_value=httpx.Response(200, json=public_jwks(key)))
        result = await keycloak_readiness_probe()
    assert result.ok is True


async def test_web_console_discovery_uses_the_discovery_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _use_discovery_url(monkeypatch, _INTERNAL)
    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{_INTERNAL}/protocol/openid-connect/token",
    }
    with respx.mock(assert_all_called=True) as router:
        router.get(_INTERNAL_DISCOVERY).mock(return_value=httpx.Response(200, json=doc))
        endpoints = await _fetch_discovery_doc(settings)
    # Browsers still go to the public address; the backplane posts in-cluster.
    assert endpoints.authorization_endpoint == doc["authorization_endpoint"]
    assert endpoints.token_endpoint == doc["token_endpoint"]


@pytest.mark.parametrize(
    ("discovery_url", "token_url"),
    [
        ("", f"{ISSUER}/protocol/openid-connect/token"),
        (_INTERNAL, f"{_INTERNAL}/protocol/openid-connect/token"),
    ],
)
def test_admin_client_token_url(
    monkeypatch: pytest.MonkeyPatch, discovery_url: str, token_url: str
) -> None:
    monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", discovery_url)
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "http://test-keycloak:8080/auth/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cret")
    get_settings.cache_clear()
    client = KeycloakAdminClient.from_settings()
    assert client._token_url == token_url
