# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the PrincipalKind discriminator on Operator (G11.2-T1 #815).

Coverage matrix (per acceptance criteria):

* :class:`~meho_backplane.auth.operator.PrincipalKind` enum values exist
  and ``USER`` is the default.
* A JWT that carries ``principal_kind=agent`` produces an
  :class:`~meho_backplane.auth.operator.Operator` with
  ``principal_kind == PrincipalKind.AGENT``.
* A JWT that carries ``principal_kind=service`` produces
  ``PrincipalKind.SERVICE``.
* A JWT whose ``principal_kind`` claim is absent produces
  ``PrincipalKind.USER`` (graceful fallback — all pre-G11.2 tokens).
* A JWT whose ``principal_kind`` claim has an unknown value is rejected
  with HTTP 401 (detail ``unknown_principal_kind``) after a
  structured-log warning — fail-closed, mirroring the unknown
  ``tenant_role`` handling at the same layer.
* A custom ``JWT_PRINCIPAL_KIND_CLAIM_NAME`` env-var shifts the claim
  lookup to the renamed field.
"""

from __future__ import annotations

import time
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
from meho_backplane.auth.operator import Operator, PrincipalKind
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
    principal_kind: str | None = None,
    claim_name: str = "principal_kind",
) -> str:
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
        if principal_kind is not None:
            payload[claim_name] = principal_kind
        header = {"alg": "RS256", "kid": key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def _make_app() -> FastAPI:
    """Minimal FastAPI app that surfaces the Operator from verify_jwt."""
    mini = FastAPI()

    @mini.get("/whoami")
    async def whoami(operator: Operator = Depends(verify_jwt)) -> dict[str, str]:
        return {
            "sub": operator.sub,
            "principal_kind": operator.principal_kind.value,
        }

    return mini


# ---------------------------------------------------------------------------
# Enum contract
# ---------------------------------------------------------------------------


def test_principal_kind_enum_values() -> None:
    """PrincipalKind has the three expected values."""
    assert PrincipalKind.USER == "user"
    assert PrincipalKind.SERVICE == "service"
    assert PrincipalKind.AGENT == "agent"


def test_operator_default_principal_kind_is_user() -> None:
    """:class:`Operator` defaults ``principal_kind`` to ``USER``."""
    import uuid

    from meho_backplane.auth.operator import TenantRole

    op = Operator(
        sub="s",
        raw_jwt="t",
        tenant_id=uuid.UUID(_TENANT_ID),
        tenant_role=TenantRole.OPERATOR,
    )
    assert op.principal_kind == PrincipalKind.USER


# ---------------------------------------------------------------------------
# Happy-path extraction
# ---------------------------------------------------------------------------


def test_agent_kind_extracted() -> None:
    """``principal_kind=agent`` claim → ``PrincipalKind.AGENT``."""
    key = _make_key("kid-agent")
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        token = _mint(key, principal_kind="agent")
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_kind"] == "agent"


def test_service_kind_extracted() -> None:
    """``principal_kind=service`` claim → ``PrincipalKind.SERVICE``."""
    key = _make_key("kid-svc")
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        token = _mint(key, principal_kind="service")
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_kind"] == "service"


def test_user_kind_extracted() -> None:
    """Explicit ``principal_kind=user`` claim → ``PrincipalKind.USER``."""
    key = _make_key("kid-user")
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        token = _mint(key, principal_kind="user")
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_kind"] == "user"


# ---------------------------------------------------------------------------
# Graceful fallback — absent claim
# ---------------------------------------------------------------------------


def test_absent_claim_defaults_to_user() -> None:
    """Tokens without ``principal_kind`` → ``PrincipalKind.USER`` (non-breaking)."""
    key = _make_key("kid-absent")
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        # principal_kind=None → claim is omitted from the token
        token = _mint(key, principal_kind=None)
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_kind"] == "user"


# ---------------------------------------------------------------------------
# Fail-closed — unknown claim value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bogus_kind", ["robot", "bogus", "USER", "Agent", ""])
def test_unknown_claim_value_rejected_with_401(
    bogus_kind: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A present-but-unrecognised ``principal_kind`` → 401 + warning log.

    ``principal_kind`` is the discriminator agent-vs-human authorization
    branches on, so an issuer-signed value outside the closed enum is
    rejected (``unknown_principal_kind``) instead of being silently
    coerced to the human-user default — the same fail-closed contract as
    an unknown ``tenant_role``. Case-variant spellings and the empty
    string are "present but unrecognised", not "absent", so they 401 too.

    structlog routes its output through :class:`PrintLoggerFactory` (stdout)
    in the test environment, so the warning shows up in ``capfd`` rather than
    ``caplog``.
    """
    key = _make_key("kid-unknown")
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        token = _mint(key, principal_kind=bogus_kind)
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "unknown_principal_kind"
    # The structured warning (claim_name + offending value) is emitted
    # before the 401 is raised; structlog emits to stdout in tests.
    out, _ = capfd.readouterr()
    assert "unknown_principal_kind" in out, (
        f"Expected 'unknown_principal_kind' in structlog stdout; got: {out!r}"
    )
    assert "principal_kind" in out


# ---------------------------------------------------------------------------
# Custom claim name via env var
# ---------------------------------------------------------------------------


def test_custom_claim_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_PRINCIPAL_KIND_CLAIM_NAME`` renames the claim the extractor reads."""
    monkeypatch.setenv("JWT_PRINCIPAL_KIND_CLAIM_NAME", "kind")
    get_settings.cache_clear()
    clear_jwks_cache()

    key = _make_key("kid-custom")
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        # Use the custom claim name "kind" in the token.
        token = _mint(key, principal_kind="agent", claim_name="kind")
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_kind"] == "agent"
