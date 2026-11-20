# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``approver`` flag on Operator and the ``approver`` claim.

* :class:`~meho_backplane.auth.operator.Operator` defaults ``approver`` to
  ``False``.
* MEHO does not read the ``approver`` claim of the token: every value, and
  a custom ``JWT_APPROVER_CLAIM_NAME``, gives ``False``. Only a login group
  whose grant gives the flag sets it.
"""

from __future__ import annotations

import time
import uuid
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth.jwt import clear_jwks_cache, verify_jwt
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"
_TENANT_ID: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars required by :class:`~meho_backplane.settings.Settings`."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _make_key(kid: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return JsonWebKey.generate_key("RSA", 2048, options={"kid": kid}, is_private=True)


def _public_jwks(key: Any) -> dict[str, Any]:
    return {"keys": [key.as_dict(is_private=False)]}


def _mint(
    key: Any,
    *,
    approver: Any = None,
    tenant_role: str = "read_only",
    claim_name: str = "approver",
) -> str:
    """Mint a signed JWT, optionally carrying an ``approver`` claim.

    ``approver`` is added to the payload verbatim only when not ``None``,
    so ``None`` models the claim-absent case (and any other value — bool,
    str, int — is emitted as-is to exercise the parser). ``tenant_role``
    defaults to ``read_only`` because the approve-only capability's whole
    point is to grant approvals access to a non-operator principal.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": "op-test",
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + 3600,
            "nbf": now,
            "tenant_id": _TENANT_ID,
            "tenant_role": tenant_role,
        }
        if approver is not None:
            payload[claim_name] = approver
        header = {"alg": "RS256", "kid": key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def _make_app() -> FastAPI:
    """Minimal FastAPI app that surfaces ``approver`` from verify_jwt."""
    mini = FastAPI()

    @mini.get("/whoami")
    async def whoami(operator: Operator = Depends(verify_jwt)) -> dict[str, bool | str]:
        return {"sub": operator.sub, "approver": operator.approver}

    return mini


def _resolve(key: Any, **mint_kwargs: Any) -> bool:
    """Mint a token, run it through verify_jwt, return the resolved flag."""
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        token = _mint(key, **mint_kwargs)
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    value = resp.json()["approver"]
    assert isinstance(value, bool)
    return value


# ---------------------------------------------------------------------------
# Model default
# ---------------------------------------------------------------------------


def test_operator_default_approver_is_false() -> None:
    """:class:`Operator` defaults ``approver`` to ``False``."""
    op = Operator(
        sub="s",
        raw_jwt="t",
        tenant_id=uuid.UUID(_TENANT_ID),
        tenant_role=TenantRole.OPERATOR,
    )
    assert op.approver is False


# ---------------------------------------------------------------------------
# The claim gives no right
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, "true", "TRUE", False, "false", 7, None])
def test_the_approver_claim_never_sets_the_flag(value: Any) -> None:
    """MEHO does not read the ``approver`` claim: every value gives ``False``."""
    assert _resolve(_make_key("kid-claim"), approver=value) is False


def test_a_custom_claim_name_does_not_set_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_APPROVER_CLAIM_NAME`` names no claim that MEHO reads."""
    monkeypatch.setenv("JWT_APPROVER_CLAIM_NAME", "can_approve")
    get_settings.cache_clear()
    clear_jwks_cache()
    assert _resolve(_make_key("kid-custom"), approver=True, claim_name="can_approve") is False
