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
    extra_claims: dict[str, Any] | None = None,
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
        if extra_claims is not None:
            payload.update(extra_claims)
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
            "client_id": operator.client_id or "",
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
# Service-account (client-credentials) classification (#3178)
# ---------------------------------------------------------------------------
#
# An OAuth2 client-credentials token carries no interactive user and often
# no ``principal_kind`` claim; it must classify as ``service`` (not the
# ``user`` default) so the #3152 standing-grants gate evaluates it. The
# positive marker is Keycloak's reserved ``service-account-<clientId>``
# username on ``preferred_username``. Fail-closed: only a positive marker
# upgrades; an explicit claim always wins; every other shape stays ``user``.


def _classify(token: str, key: Any, *, monkeypatch: pytest.MonkeyPatch | None = None) -> str:
    """Drive a token through ``verify_jwt`` and return the resolved kind."""
    if monkeypatch is not None:
        get_settings.cache_clear()
        clear_jwks_cache()
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["principal_kind"]


def test_client_credentials_marker_classifies_service() -> None:
    """No ``principal_kind`` claim + ``service-account-*`` username → ``service``."""
    key = _make_key("kid-svc-marker")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "service-account-deploy-bot"},
    )
    assert _classify(token, key) == "service"


def test_ordinary_username_without_marker_stays_user() -> None:
    """No claim + a non-service-account username → ``user`` (fail-closed default)."""
    key = _make_key("kid-user-marker")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "alice"},
    )
    assert _classify(token, key) == "user"


def test_explicit_user_claim_overrides_service_marker() -> None:
    """An explicit ``principal_kind=user`` wins even when the marker is present.

    The marker inference runs **only** for the absent-claim case, so an
    issuer that deliberately stamps ``user`` is never silently upgraded.
    """
    key = _make_key("kid-explicit-user")
    token = _mint(
        key,
        principal_kind="user",
        extra_claims={"preferred_username": "service-account-deploy-bot"},
    )
    assert _classify(token, key) == "user"


def test_custom_service_account_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_SERVICE_ACCOUNT_USERNAME_PREFIX`` retargets the marker prefix."""
    monkeypatch.setenv("JWT_SERVICE_ACCOUNT_USERNAME_PREFIX", "svc:")
    key = _make_key("kid-custom-prefix")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "svc:deploy-bot"},
    )
    assert _classify(token, key, monkeypatch=monkeypatch) == "service"


def test_custom_service_account_username_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_SERVICE_ACCOUNT_USERNAME_CLAIM`` retargets which claim carries the marker."""
    monkeypatch.setenv("JWT_SERVICE_ACCOUNT_USERNAME_CLAIM", "client_id")
    key = _make_key("kid-custom-claim")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"client_id": "service-account-deploy-bot"},
    )
    assert _classify(token, key, monkeypatch=monkeypatch) == "service"


def test_empty_prefix_disables_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty configured prefix disables the marker — a service account stays ``user``."""
    monkeypatch.setenv("JWT_SERVICE_ACCOUNT_USERNAME_PREFIX", "")
    key = _make_key("kid-empty-prefix")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "service-account-deploy-bot"},
    )
    assert _classify(token, key, monkeypatch=monkeypatch) == "user"


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


# ---------------------------------------------------------------------------
# clientId recovery for the add-on parent-linkage seam (#3028)
# ---------------------------------------------------------------------------
#
# A paired add-on's Keycloak service-account username is
# ``service-account-<clientId>``; stripping the reserved prefix recovers the
# ``clientId`` (== ``addon_pairing.keycloak_client_id``), which the #3028
# out-of-process parent-linkage matches a dispatch's principal against.


def _client_id(token: str, key: Any) -> str:
    """Drive a token through ``verify_jwt`` and return the resolved client_id."""
    app = _make_app()
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_public_jwks(key)))
        with TestClient(app) as client:
            resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["client_id"]


def test_client_id_recovered_from_service_account_username() -> None:
    """A ``service-account-addon:<name>`` username → client_id ``addon:<name>``."""
    key = _make_key("kid-cid-svc")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "service-account-addon:automation"},
    )
    assert _client_id(token, key) == "addon:automation"


def test_client_id_absent_for_ordinary_user() -> None:
    """An interactive user's username has no service-account marker → empty client_id."""
    key = _make_key("kid-cid-user")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "alice"},
    )
    assert _client_id(token, key) == ""


def test_client_id_uses_custom_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clientId derivation strips the configured (custom) prefix."""
    monkeypatch.setenv("JWT_SERVICE_ACCOUNT_USERNAME_PREFIX", "svc:")
    get_settings.cache_clear()
    clear_jwks_cache()
    key = _make_key("kid-cid-prefix")
    token = _mint(
        key,
        principal_kind=None,
        extra_claims={"preferred_username": "svc:addon:ssp"},
    )
    assert _client_id(token, key) == "addon:ssp"


def test_extract_client_id_unit() -> None:
    """``_extract_client_id`` reads the marker directly off a claims mapping."""
    from meho_backplane.auth.jwt import _extract_client_id

    settings = get_settings()
    assert (
        _extract_client_id({"preferred_username": "service-account-addon:automation"}, settings)
        == "addon:automation"
    )
    assert _extract_client_id({"preferred_username": "alice"}, settings) is None
    assert _extract_client_id({}, settings) is None
    # A bare prefix with no clientId body resolves to None, not "".
    assert _extract_client_id({"preferred_username": "service-account-"}, settings) is None
