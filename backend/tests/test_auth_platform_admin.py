# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the cross-tenant ``platform_admin`` flag on Operator (#1638).

Coverage matrix (per acceptance criteria):

* :class:`~meho_backplane.auth.operator.Operator` defaults
  ``platform_admin`` to ``False``.
* A JWT carrying ``platform_admin=true`` (JSON boolean) produces an
  :class:`Operator` with ``platform_admin is True``; ``false`` → ``False``.
* String shapes ``"true"`` / ``"false"`` (a realm whose mapper emits the
  claim as a string) are honoured.
* A JWT whose ``platform_admin`` claim is absent → ``False`` (graceful,
  fail-closed — every pre-existing token).
* An agent token (``principal_kind=agent``, no ``platform_admin`` claim)
  → ``False`` (agents are never platform-admin on the strength of their
  ``tenant_admin`` role).
* A malformed ``platform_admin`` value (a number, an unrecognised string)
  → ``False`` with a structured-log warning (no exception).
* A custom ``JWT_PLATFORM_ADMIN_CLAIM_NAME`` env-var shifts the lookup.
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
    platform_admin: Any = None,
    principal_kind: str | None = None,
    claim_name: str = "platform_admin",
) -> str:
    """Mint a signed JWT, optionally carrying ``platform_admin`` / ``principal_kind``.

    ``platform_admin`` is added to the payload verbatim only when not
    ``None``, so ``None`` models the claim-absent case (and any other
    value — bool, str, int — is emitted as-is to exercise the parser).
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
            "tenant_role": "operator",
        }
        if platform_admin is not None:
            payload[claim_name] = platform_admin
        if principal_kind is not None:
            payload["principal_kind"] = principal_kind
        header = {"alg": "RS256", "kid": key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def _make_app() -> FastAPI:
    """Minimal FastAPI app that surfaces ``platform_admin`` from verify_jwt."""
    mini = FastAPI()

    @mini.get("/whoami")
    async def whoami(operator: Operator = Depends(verify_jwt)) -> dict[str, bool | str]:
        return {"sub": operator.sub, "platform_admin": operator.platform_admin}

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
    value = resp.json()["platform_admin"]
    assert isinstance(value, bool)
    return value


# ---------------------------------------------------------------------------
# Model default
# ---------------------------------------------------------------------------


def test_operator_default_platform_admin_is_false() -> None:
    """:class:`Operator` defaults ``platform_admin`` to ``False``."""
    op = Operator(
        sub="s",
        raw_jwt="t",
        tenant_id=uuid.UUID(_TENANT_ID),
        tenant_role=TenantRole.OPERATOR,
    )
    assert op.platform_admin is False


# ---------------------------------------------------------------------------
# Boolean claim extraction
# ---------------------------------------------------------------------------


def test_platform_admin_true_extracted() -> None:
    """``platform_admin=true`` (JSON boolean) → ``True``."""
    assert _resolve(_make_key("kid-true"), platform_admin=True) is True


def test_platform_admin_false_extracted() -> None:
    """``platform_admin=false`` (JSON boolean) → ``False``."""
    assert _resolve(_make_key("kid-false"), platform_admin=False) is False


def test_platform_admin_string_true_extracted() -> None:
    """A realm emitting the claim as the string ``"true"`` → ``True``."""
    assert _resolve(_make_key("kid-strue"), platform_admin="true") is True


def test_platform_admin_string_false_extracted() -> None:
    """The string ``"false"`` → ``False``."""
    assert _resolve(_make_key("kid-sfalse"), platform_admin="false") is False


# ---------------------------------------------------------------------------
# Graceful / fail-closed paths
# ---------------------------------------------------------------------------


def test_absent_claim_defaults_to_false() -> None:
    """Tokens without ``platform_admin`` → ``False`` (non-breaking, fail-closed)."""
    assert _resolve(_make_key("kid-absent")) is False


def test_agent_token_is_not_platform_admin() -> None:
    """An agent token (``principal_kind=agent``, no flag) → ``False``.

    Agents authenticate with ``tenant_admin`` role; they must not be
    mistaken for platform operators absent an explicit claim.
    """
    assert _resolve(_make_key("kid-agent"), principal_kind="agent") is False


def test_malformed_claim_value_defaults_to_false(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A non-boolean ``platform_admin`` value → ``False`` + warning log.

    structlog routes through ``PrintLoggerFactory`` (stdout) in tests, so
    the warning surfaces in ``capfd`` rather than ``caplog``.
    """
    value = _resolve(_make_key("kid-malformed"), platform_admin=7)
    assert value is False
    out, _ = capfd.readouterr()
    assert "malformed_platform_admin_claim" in out, (
        f"Expected 'malformed_platform_admin_claim' in structlog stdout; got: {out!r}"
    )


# ---------------------------------------------------------------------------
# Custom claim name via env var
# ---------------------------------------------------------------------------


def test_custom_claim_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_PLATFORM_ADMIN_CLAIM_NAME`` renames the claim the extractor reads."""
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_CLAIM_NAME", "is_platform_admin")
    get_settings.cache_clear()
    clear_jwks_cache()
    assert (
        _resolve(
            _make_key("kid-custom"),
            platform_admin=True,
            claim_name="is_platform_admin",
        )
        is True
    )
