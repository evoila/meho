# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the approve-only ``approver`` capability flag on Operator (#3243).

Coverage matrix (mirrors ``test_auth_platform_admin.py`` — the sibling
orthogonal-capability primitive):

* :class:`~meho_backplane.auth.operator.Operator` defaults ``approver`` to
  ``False``.
* A JWT carrying ``approver=true`` (JSON boolean) produces an
  :class:`Operator` with ``approver is True``; ``false`` → ``False``.
* String shapes ``"true"`` / ``"false"`` (a realm whose mapper emits the
  claim as a string) are honoured.
* A JWT whose ``approver`` claim is absent → ``False`` (graceful,
  fail-closed — every pre-existing token).
* A malformed ``approver`` value (a number) → ``False`` with a
  structured-log warning (no exception).
* A custom ``JWT_APPROVER_CLAIM_NAME`` env-var shifts the lookup.
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
# Boolean claim extraction
# ---------------------------------------------------------------------------


def test_approver_true_extracted() -> None:
    """``approver=true`` (JSON boolean) → ``True``."""
    assert _resolve(_make_key("kid-true"), approver=True) is True


def test_approver_false_extracted() -> None:
    """``approver=false`` (JSON boolean) → ``False``."""
    assert _resolve(_make_key("kid-false"), approver=False) is False


def test_approver_string_true_extracted() -> None:
    """A realm emitting the claim as the string ``"true"`` → ``True``."""
    assert _resolve(_make_key("kid-strue"), approver="true") is True


def test_approver_string_false_extracted() -> None:
    """The string ``"false"`` → ``False``."""
    assert _resolve(_make_key("kid-sfalse"), approver="false") is False


# ---------------------------------------------------------------------------
# Graceful / fail-closed paths
# ---------------------------------------------------------------------------


def test_absent_claim_defaults_to_false() -> None:
    """Tokens without ``approver`` → ``False`` (non-breaking, fail-closed)."""
    assert _resolve(_make_key("kid-absent")) is False


def test_malformed_claim_value_defaults_to_false(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A non-boolean ``approver`` value → ``False`` + warning log.

    structlog routes through ``PrintLoggerFactory`` (stdout) in tests, so
    the warning surfaces in ``capfd`` rather than ``caplog``.
    """
    value = _resolve(_make_key("kid-malformed"), approver=7)
    assert value is False
    out, _ = capfd.readouterr()
    assert "malformed_approver_claim" in out, (
        f"Expected 'malformed_approver_claim' in structlog stdout; got: {out!r}"
    )


# ---------------------------------------------------------------------------
# Custom claim name via env var
# ---------------------------------------------------------------------------


def test_custom_claim_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_APPROVER_CLAIM_NAME`` renames the claim the extractor reads."""
    monkeypatch.setenv("JWT_APPROVER_CLAIM_NAME", "can_approve")
    get_settings.cache_clear()
    clear_jwks_cache()
    assert (
        _resolve(
            _make_key("kid-custom"),
            approver=True,
            claim_name="can_approve",
        )
        is True
    )
