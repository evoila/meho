# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Automation tenant-plane login shapes across 9.0 and 9.1 (evoila/meho#3865).

On VCFA 9.1 ``POST /iaas/api/login`` validates only ``{"refreshToken": ...}``
and answers a username/password body with HTTP 400 (``'refreshToken' can not
be null.``). :func:`meho_backplane.connectors.vcf_automation._auth.tenant_login`
therefore carries both shapes:

* legacy (9.0): ``{username, password[, domain]}`` → ``{"token"}``;
* token exchange (9.1+): the secret's optional ``refresh_token``, else a CSP
  mint (``POST /csp/gateway/am/api/login?access_token``) → ``refresh_token``,
  then ``POST /iaas/api/login`` with ``{"refreshToken"}`` → ``{"token"}``.

The fake HTTP layer is respx; every test drives the connector's own
transport (``auth_headers`` / ``_request_json``) so the per-target bearer
cache and the data-path 401 re-mint are exercised, not just the helper.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_automation import (
    VcfAutomationConnector,
    VcfAutomationTargetLike,
)
from meho_backplane.connectors.vcf_automation import session as session_module

_HOST = "vcfa-login.test.invalid"
_BASE_URL = f"https://{_HOST}"
_LOGIN = "/iaas/api/login"
_CSP = "/csp/gateway/am/api/login"
_LEGACY_400 = {
    "message": "'refreshToken' can not be null.",
    "statusCode": 400,
    "errorCode": 0,
}


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vcfa-login"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vcfa/login"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    fqdn: str | None = None
    domain: str | None = None
    provider_username: str | None = None
    provider_secret_ref: str | None = None
    tls_server_name: str | None = None
    version: str | None = None
    fingerprint: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _connector(creds: dict[str, str] | None = None) -> VcfAutomationConnector:
    pair = creds or {"username": "svc-meho", "password": "stub-password"}

    async def _loader(_t: VcfAutomationTargetLike, _o: Operator) -> dict[str, str]:
        return dict(pair)

    return VcfAutomationConnector(credentials_loader=_loader)


def _body(route: respx.Route, index: int = 0) -> Any:
    return json.loads(route.calls[index].request.content.decode())


# ---------------------------------------------------------------------------
# Version-selected paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unversioned_target_uses_legacy_password_body() -> None:
    """VCFA 9.0 shape: one POST /iaas/api/login with username/password, no CSP call."""
    connector = _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_LOGIN).respond(200, json={"token": "legacy-token"})
        csp = mock.post(_CSP).respond(404)
        headers = await connector.auth_headers(_Target(), _operator(), path="/iaas/api/projects")

    assert headers["Authorization"] == "Bearer legacy-token"
    assert _body(login) == {"username": "svc-meho", "password": "stub-password"}
    assert login.call_count == 1
    assert not csp.called
    await connector.aclose()


@pytest.mark.parametrize(
    ("version", "fingerprint"),
    [
        ("9.1", None),
        (None, {"version": "9.1.0.0"}),
        ("9.2.1", None),
    ],
)
@pytest.mark.asyncio
async def test_91_target_goes_straight_to_csp_token_exchange(
    version: str | None, fingerprint: dict[str, Any] | None
) -> None:
    """A >=9.1 target never sends the password body to /iaas/api/login."""
    connector = _connector()
    target = _Target(version=version, fingerprint=fingerprint)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post(_CSP).respond(200, json={"refresh_token": "minted-refresh"})
        login = mock.post(_LOGIN).respond(200, json={"tokenType": "Bearer", "token": "bearer-91"})
        headers = await connector.auth_headers(target, _operator(), path="/iaas/api/projects")

    assert headers["Authorization"] == "Bearer bearer-91"
    assert csp.call_count == 1
    csp_request = csp.calls[0].request
    assert "access_token" in csp_request.url.params
    assert _body(csp) == {"username": "svc-meho", "password": "stub-password"}
    assert login.call_count == 1
    assert _body(login) == {"refreshToken": "minted-refresh"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_api_date_fingerprint_version_is_not_mistaken_for_a_release() -> None:
    """The fingerprint's API-date label (``2021-07-15``) keeps the legacy-first order."""
    connector = _connector()
    target = _Target(fingerprint={"version": "2021-07-15"})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_LOGIN).respond(200, json={"token": "legacy-token"})
        await connector.auth_headers(target, _operator(), path="/iaas/api/projects")

    assert _body(login) == {"username": "svc-meho", "password": "stub-password"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# Fallback on 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_400_falls_back_to_csp_token_exchange() -> None:
    """Unversioned target on a 9.1 appliance: legacy 400 → CSP mint → refreshToken exchange."""
    connector = _connector()
    target = _Target()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_LOGIN)
        login.side_effect = [
            httpx.Response(400, json=_LEGACY_400),
            httpx.Response(200, json={"token": "bearer-after-fallback"}),
        ]
        csp = mock.post(_CSP).respond(200, json={"refresh_token": "minted-refresh"})
        headers = await connector.auth_headers(target, _operator(), path="/iaas/api/projects")

    assert headers["Authorization"] == "Bearer bearer-after-fallback"
    assert _body(login, 0) == {"username": "svc-meho", "password": "stub-password"}
    assert _body(login, 1) == {"refreshToken": "minted-refresh"}
    assert csp.call_count == 1
    assert connector._tenant_tokens == {target_cache_key(target): "bearer-after-fallback"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_legacy_401_does_not_fall_back() -> None:
    """A 401 on the password body is a stale credential, not a shape mismatch."""
    connector = _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_LOGIN).respond(401)
        csp = mock.post(_CSP).respond(200, json={"refresh_token": "unused"})
        with pytest.raises(ConnectorAuthError) as exc_info:
            await connector.auth_headers(_Target(), _operator(), path="/iaas/api/projects")

    assert exc_info.value.cause == "session_establish_401"
    assert not csp.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_fallback_csp_404_raises_structured_error_naming_refresh_token_field() -> None:
    """No usable password→token exchange: structured auth error + remediation + upstream body."""
    connector = _connector()
    target = _Target()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_LOGIN).respond(400, json=_LEGACY_400)
        mock.post(_CSP).respond(404, text="Not Found")
        with pytest.raises(ConnectorAuthError) as exc_info:
            await connector.auth_headers(target, _operator(), path="/iaas/api/projects")

    err = exc_info.value
    assert err.cause == "session_establish_404"
    assert err.status_code == 404
    assert err.target_name == "vcfa-login"
    message = str(err)
    assert "'refresh_token'" in message
    assert "'refreshToken' can not be null." in message
    assert "vcfa/login" in message
    assert err.remediation is not None
    assert err.remediation.startswith("Create an API token in VCF Automation")
    assert "'refresh_token'" in err.remediation
    assert connector._tenant_tokens == {}
    await connector.aclose()


# ---------------------------------------------------------------------------
# Operator-supplied API token (secret field ``refresh_token``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_refresh_token_is_exchanged_directly() -> None:
    """``refresh_token`` in the secret → one refreshToken exchange, no password body, no CSP."""
    connector = _connector(
        {"username": "svc-meho", "password": "stub-password", "refresh_token": "api-token"}
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_LOGIN).respond(200, json={"token": "bearer-from-api-token"})
        csp = mock.post(_CSP).respond(404)
        headers = await connector.auth_headers(_Target(), _operator(), path="/iaas/api/projects")

    assert headers["Authorization"] == "Bearer bearer-from-api-token"
    assert login.call_count == 1
    assert _body(login) == {"refreshToken": "api-token"}
    assert not csp.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_refused_refresh_token_raises_structured_error() -> None:
    """A refused API token (400 on the exchange) is a restage-class auth failure."""
    connector = _connector(
        {"username": "svc-meho", "password": "stub-password", "refresh_token": "expired"}
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_LOGIN).respond(400, json={"message": "Invalid refresh token"})
        with pytest.raises(ConnectorAuthError) as exc_info:
            await connector.auth_headers(_Target(), _operator(), path="/iaas/api/projects")

    assert exc_info.value.cause == "session_establish_400"
    assert "'refresh_token'" in str(exc_info.value)
    assert exc_info.value.remediation is not None
    assert "'refresh_token'" in exc_info.value.remediation
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_surfaces_optional_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Vault loader keeps username/password required and passes refresh_token through."""
    secret: dict[str, object] = {
        "username": "svc-meho",
        "password": "pw\n",
        "refresh_token": " api-token\n",
    }

    async def _fake_read(_target: object, _operator: Operator) -> dict[str, object]:
        return dict(secret)

    monkeypatch.setattr(session_module, "load_vault_secret_data", _fake_read)
    creds = await session_module.load_credentials_from_vault(_Target(), _operator())
    assert creds == {"username": "svc-meho", "password": "pw", "refresh_token": "api-token"}

    secret.pop("refresh_token")
    creds = await session_module.load_credentials_from_vault(_Target(), _operator())
    assert creds == {"username": "svc-meho", "password": "pw"}


@pytest.mark.asyncio
async def test_default_loader_still_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret carrying only ``refresh_token`` fails closed: the provider plane needs the pair."""
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError

    async def _fake_read(_target: object, _operator: Operator) -> dict[str, object]:
        return {"username": "svc-meho", "refresh_token": "api-token"}

    monkeypatch.setattr(session_module, "load_vault_secret_data", _fake_read)
    with pytest.raises(VaultCredentialsReadError, match="password"):
        await session_module.load_credentials_from_vault(_Target(), _operator())


# ---------------------------------------------------------------------------
# Domain source + data-path 401 re-mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_is_read_from_target_extras() -> None:
    """A DB-backed target has no ``domain`` column; ``extras["domain"]`` feeds the login."""
    connector = _connector()
    target = _Target(version="9.1", extras={"domain": "corp.example"})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post(_CSP).respond(200, json={"refresh_token": "r"})
        mock.post(_LOGIN).respond(200, json={"token": "t"})
        await connector.auth_headers(target, _operator(), path="/iaas/api/projects")

    assert _body(csp)["domain"] == "corp.example"
    await connector.aclose()


@pytest.mark.asyncio
async def test_data_path_401_re_mints_through_the_token_exchange() -> None:
    """An expired bearer (401) evicts the cache and re-runs CSP mint + exchange once."""
    connector = _connector()
    target = _Target(version="9.1")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post(_CSP)
        csp.side_effect = [
            httpx.Response(200, json={"refresh_token": "r1"}),
            httpx.Response(200, json={"refresh_token": "r2"}),
        ]
        login = mock.post(_LOGIN)
        login.side_effect = [
            httpx.Response(200, json={"token": "stale"}),
            httpx.Response(200, json={"token": "fresh"}),
        ]
        projects = mock.get("/iaas/api/projects")
        projects.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"content": [{"name": "default"}]}),
        ]
        result = await connector._request_json(
            target, "GET", "/iaas/api/projects", operator=_operator()
        )

    assert result == {"content": [{"name": "default"}]}
    assert csp.call_count == 2
    assert [_body(login, i) for i in range(2)] == [{"refreshToken": "r1"}, {"refreshToken": "r2"}]
    assert projects.calls[0].request.headers["authorization"] == "Bearer stale"
    assert projects.calls[1].request.headers["authorization"] == "Bearer fresh"
    assert connector._tenant_tokens == {target_cache_key(target): "fresh"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_bearer_cached_per_target_across_calls() -> None:
    """The minted bearer is reused: two tenant calls, one CSP mint, one exchange."""
    connector = _connector()
    target = _Target(version="9.1")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post(_CSP).respond(200, json={"refresh_token": "r"})
        login = mock.post(_LOGIN).respond(200, json={"token": "t"})
        await connector.auth_headers(target, _operator(), path="/iaas/api/projects")
        await connector.auth_headers(target, _operator(), path="/iaas/api/deployments")

    assert csp.call_count == 1
    assert login.call_count == 1
    await connector.aclose()


def test_auth_failed_envelope_uses_connector_remediation_not_restage() -> None:
    """The ``connector_auth_failed`` builder prefers the error's own remediation (#3865)."""
    from meho_backplane.operations._errors import result_connector_auth_failed

    request = httpx.Request("POST", f"{_BASE_URL}{_CSP}")
    response = httpx.Response(404, text="Not Found", request=request)
    cause = httpx.HTTPStatusError("404", request=request, response=response)
    remediation = "Create an API token in VCF Automation and store it as 'refresh_token'."
    try:
        raise ConnectorAuthError(
            "vcf-automation tenant session establish failed",
            status_code=404,
            cause="session_establish_404",
            target_name="vcfa-login",
            secret_ref="vcfa/login",
            remediation=remediation,
        ) from cause
    except ConnectorAuthError as exc:
        result = result_connector_auth_failed("vcfa.tenant.project.list", exc, _Target(), 1.0)

    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["remediation"] == remediation
    assert remediation in (result.error or "")
    assert "restage" not in (result.error or "").lower()


def test_auth_failed_envelope_keeps_restage_remediation_by_default() -> None:
    """Without a connector remediation the stale-credential restage text is unchanged."""
    from meho_backplane.operations._errors import result_connector_auth_failed

    exc = ConnectorAuthError(
        "login rejected",
        status_code=401,
        cause="session_establish_401",
        target_name="vcfa-login",
        secret_ref="vcfa/login",
    )
    result = result_connector_auth_failed("vcfa.tenant.project.list", exc, _Target(), 1.0)
    assert "refresh_token" not in result.extras["remediation"]
    assert "vcfa/login" in result.extras["remediation"]
