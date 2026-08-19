# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`Vra8Connector` two-step session mint + fingerprint/probe.

Exercises the load-bearing vRA 8 auth contract — the CSP-identity → IaaS
token-exchange flow:

* ``POST /csp/gateway/am/api/login?access_token`` with ``{username, password,
  domain?}`` returns ``{"refresh_token": ...}`` (snake_case) in the body;
* ``POST /iaas/api/login`` with ``{"refreshToken": ...}`` (camelCase) returns
  ``{"tokenType": "Bearer", "token": ...}`` — the bearer the connector caches and
  sends as ``Authorization: Bearer <token>`` with ``Accept: application/json`` on
  every read (one bearer authenticates all vRA 8 services).
* Per-target token caching + tenant-unique isolation.
* Both dispatch-path eviction hooks (``invalidate_session`` #2067 /
  ``invalidate_credentials`` #2396) → re-mint.
* ``GET /iaas/api/about`` unauthenticated fingerprint/probe (version left ``None``).
* ``auth_model`` gating + empty-``raw_jwt`` fail-closed.

Adapts :mod:`tests.test_connectors_vcd_auth` to the two-step (body-token) flow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vra8 import (
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_VERSION,
    Vra8Connector,
    Vra8TargetLike,
)
from meho_backplane.settings import get_settings

_CSP_LOGIN_PATH = "/csp/gateway/am/api/login"
_IAAS_LOGIN_PATH = "/iaas/api/login"
_ABOUT_PATH = "/iaas/api/about"
_PROJECTS_PATH = "/iaas/api/projects"


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Return a minimal :class:`Operator`; non-empty ``raw_jwt`` passes the fail-closed gate."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _chassis_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env the shared credential loader reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_vra8_registry() -> Iterator[None]:
    """Restore Vra8Connector's package registration (the versioned triple only).

    Sibling tests clear the shared registry between tests; this fixture restores
    exactly what the ``vra8`` package's ``__init__`` registers — the versioned triple
    and NO wildcard (the modern ``vcf_automation`` owns ``("vcfa","","")``).
    """
    clear_registry()
    register_connector_v2(
        product=Vra8Connector.product,
        version=Vra8Connector.version,
        impl_id=Vra8Connector.impl_id,
        cls=Vra8Connector,
    )
    yield
    clear_registry()


@dataclass
class _StubTarget:
    """Target satisfying :data:`Vra8TargetLike` structurally."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(name="vra-a", host="vra-a.test.invalid", port=443, secret_ref="vra/vra-a")
_TARGET_B = _StubTarget(name="vra-b", host="vra-b.test.invalid", port=443, secret_ref="vra/vra-b")


async def _stub_loader(_target: Vra8TargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target / operator."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> Vra8Connector:
    """Build a connector wired with the stub loader."""
    return Vra8Connector(credentials_loader=_stub_loader)


def _mock_two_step_login(
    mock: respx.Router,
    *,
    refresh_token: str = "refresh-abc",
    bearer: str = "bearer-xyz",
) -> tuple[respx.Route, respx.Route]:
    """Register the CSP + IaaS login routes returning the given tokens.

    CSP returns ``{refresh_token}`` (snake_case), IaaS returns ``{tokenType, token}``.
    """
    csp = mock.post(_CSP_LOGIN_PATH).respond(200, json={"refresh_token": refresh_token})
    iaas = mock.post(_IAAS_LOGIN_PATH).respond(200, json={"tokenType": "Bearer", "token": bearer})
    return csp, iaas


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vra8_connector_subclasses_http_connector() -> None:
    """The connector inherits from HttpConnector with the legacy-dual-impl metadata."""
    assert issubclass(Vra8Connector, HttpConnector)
    assert Vra8Connector.product == "vcfa"
    assert Vra8Connector.version == "8.0"
    assert Vra8Connector.impl_id == "vcfa-vra8"
    assert Vra8Connector.supported_version_range == ">=8.0,<9.0"
    assert Vra8Connector.priority == 1


def test_importing_package_registers_versioned_triple_only() -> None:
    """The vra8 package registers its versioned triple and NOT the ``("vcfa","","")`` wildcard.

    The modern ``vcf_automation`` owns the wildcard; the legacy impl registers only
    its versioned triple (the autouse fixture restores exactly that).
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    assert registry[(VRA8_PRODUCT, VRA8_VERSION, VRA8_IMPL_ID)] is Vra8Connector
    assert (VRA8_PRODUCT, "", "") not in registry


# ---------------------------------------------------------------------------
# Two-step session mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_mints_bearer_via_two_step_login() -> None:
    """A read runs CSP login → refresh_token → IaaS exchange → bearer, then the data GET."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        csp, iaas = _mock_two_step_login(mock, refresh_token="rt-1", bearer="bt-1")
        projects = mock.get(_PROJECTS_PATH).respond(200, json={"content": [], "totalElements": 0})
        result = await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())

    assert result == {"content": [], "totalElements": 0}
    # Step 1 — CSP login: username/password body + the ?access_token flag.
    csp_req = csp.calls[0].request
    assert "access_token" in dict(csp_req.url.params)
    import json as _json

    csp_body = _json.loads(csp_req.content)
    assert csp_body == {"username": "svc-meho", "password": "stub-password"}
    # Step 2 — IaaS exchange: the refresh_token rides back as camelCase refreshToken.
    iaas_body = _json.loads(iaas.calls[0].request.content)
    assert iaas_body == {"refreshToken": "rt-1"}
    # The data GET carried the minted bearer + the constant JSON Accept.
    data_req = projects.calls[0].request
    assert data_req.headers["Authorization"] == "Bearer bt-1"
    assert data_req.headers["Accept"] == "application/json"
    await connector.aclose()


@pytest.mark.asyncio
async def test_csp_domain_forwarded_when_extras_set_it() -> None:
    """``extras["domain"]`` is forwarded in the CSP login body (directory/AD users).

    The production ``Target`` model has no ``domain`` column, so the selector rides
    the per-product ``extras`` escape hatch (like ``vcd``'s ``vcd_api_version``);
    the target here is shaped like production — no ``domain`` attribute.
    """
    target = _StubTarget(
        name="vra-dom",
        host="vra-dom.test.invalid",
        port=443,
        secret_ref="vra/dom",
        extras={"domain": "corp.example"},
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-dom.test.invalid") as mock:
        csp, _iaas = _mock_two_step_login(mock)
        mock.get(_ABOUT_PATH).respond(200, json={"latestApiVersion": "2021-07-15"})
        await connector._vra_get(target, _ABOUT_PATH, operator=_make_operator())

    import json as _json

    body = _json.loads(csp.calls[0].request.content)
    assert body["domain"] == "corp.example"
    await connector.aclose()


@pytest.mark.asyncio
async def test_token_cached_across_calls_against_same_target() -> None:
    """Two reads against the same target run the two-step login once."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        csp, iaas = _mock_two_step_login(mock, bearer="bt-cache")
        mock.get(_PROJECTS_PATH).respond(200, json={"content": []})
        mock.get(_ABOUT_PATH).respond(200, json={"latestApiVersion": "2021-07-15"})
        await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
        await connector._vra_get(_TARGET_A, _ABOUT_PATH, operator=_make_operator())

    assert csp.call_count == 1
    assert iaas.call_count == 1
    assert connector._session_tokens == {target_cache_key(_TARGET_A): "bt-cache"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_tokens_separate() -> None:
    """Two targets get two distinct cached bearers."""
    connector = _make_connector()

    async with respx.mock() as mock:
        mock.post("https://vra-a.test.invalid" + _CSP_LOGIN_PATH).respond(
            200, json={"refresh_token": "rt-a"}
        )
        mock.post("https://vra-a.test.invalid" + _IAAS_LOGIN_PATH).respond(
            200, json={"token": "bt-a"}
        )
        mock.post("https://vra-b.test.invalid" + _CSP_LOGIN_PATH).respond(
            200, json={"refresh_token": "rt-b"}
        )
        mock.post("https://vra-b.test.invalid" + _IAAS_LOGIN_PATH).respond(
            200, json={"token": "bt-b"}
        )
        mock.get("https://vra-a.test.invalid" + _PROJECTS_PATH).respond(200, json={"content": []})
        mock.get("https://vra-b.test.invalid" + _PROJECTS_PATH).respond(200, json={"content": []})
        await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
        await connector._vra_get(_TARGET_B, _PROJECTS_PATH, operator=_make_operator())

    assert connector._session_tokens == {
        target_cache_key(_TARGET_A): "bt-a",
        target_cache_key(_TARGET_B): "bt-b",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_tokens() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached token (#1642/#1672)."""
    connector = _make_connector()
    tenant_one = _StubTarget(
        name="vra-shared",
        host="vra-shared.test.invalid",
        port=443,
        secret_ref="vra/shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="vra-shared",
        host="vra-shared.test.invalid",
        port=443,
        secret_ref="vra/shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    async with respx.mock(base_url="https://vra-shared.test.invalid") as mock:
        mock.post(_CSP_LOGIN_PATH).respond(200, json={"refresh_token": "rt"})
        mock.post(_IAAS_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"token": "bt-one"}),
                httpx.Response(200, json={"token": "bt-two"}),
            ]
        )
        mock.get(_PROJECTS_PATH).respond(200, json={"content": []})
        await connector._vra_get(tenant_one, _PROJECTS_PATH, operator=_make_operator())
        await connector._vra_get(tenant_two, _PROJECTS_PATH, operator=_make_operator())

    assert connector._session_tokens == {
        target_cache_key(tenant_one): "bt-one",
        target_cache_key(tenant_two): "bt-two",
    }
    await connector.aclose()


# ---------------------------------------------------------------------------
# Login failure modes — both steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csp_login_401_surfaces_connector_auth_error_naming_target() -> None:
    """401 from the CSP login raises the structured ConnectorAuthError (#2329)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.post(_CSP_LOGIN_PATH).respond(401)
        with pytest.raises(RuntimeError, match=r"vra-a") as exc_info:
            await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
    err = exc_info.value
    assert isinstance(err, ConnectorAuthError)
    assert "401" in str(err)
    assert "CSP" in str(err)
    await connector.aclose()


@pytest.mark.asyncio
async def test_iaas_exchange_403_surfaces_connector_auth_error_naming_target() -> None:
    """403 from the IaaS token exchange raises the structured ConnectorAuthError (#2329)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.post(_CSP_LOGIN_PATH).respond(200, json={"refresh_token": "rt"})
        mock.post(_IAAS_LOGIN_PATH).respond(403)
        with pytest.raises(RuntimeError, match=r"vra-a") as exc_info:
            await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
    err = exc_info.value
    assert isinstance(err, ConnectorAuthError)
    assert "403" in str(err)
    assert "IaaS" in str(err)
    await connector.aclose()


@pytest.mark.asyncio
async def test_csp_missing_refresh_token_raises() -> None:
    """A 2xx CSP login with no ``refresh_token`` raises naming the target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.post(_CSP_LOGIN_PATH).respond(200, json={"not_a_token": "x"})
        with pytest.raises(RuntimeError, match=r"vra-a") as exc_info:
            await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
    assert "refresh_token" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_iaas_missing_token_raises() -> None:
    """A 2xx IaaS exchange with no ``token`` field raises naming the target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.post(_CSP_LOGIN_PATH).respond(200, json={"refresh_token": "rt"})
        mock.post(_IAAS_LOGIN_PATH).respond(200, json={"tokenType": "Bearer"})
        with pytest.raises(RuntimeError, match=r"vra-a") as exc_info:
            await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
    assert "token" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_raises_clear_error() -> None:
    """A loader returning a dict without ``password`` raises naming the target."""

    async def _bad_loader(_t: Vra8TargetLike, _o: Operator) -> dict[str, str]:
        return {"username": "svc-meho"}  # missing 'password' on purpose

    connector = Vra8Connector(credentials_loader=_bad_loader)
    async with respx.mock(base_url="https://vra-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"password"):
            await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth-model gating + fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation / unknown modes raise NotImplementedError naming target + mode."""
    target = _StubTarget(
        name="vra-per-user",
        host="vra.test.invalid",
        port=443,
        secret_ref="vra/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())
    assert "vra-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model() -> None:
    """``auth_model=None`` (pre-G0.3) is accepted."""
    target = _StubTarget(
        name="vra-pre-g03", host="vra.test.invalid", port=443, secret_ref="vra/pre", auth_model=None
    )
    connector = _make_connector()
    async with respx.mock(base_url="https://vra.test.invalid") as mock:
        _mock_two_step_login(mock, bearer="pre-bt")
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "Bearer pre-bt"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_establish_rejects_empty_operator_jwt() -> None:
    """A system-initiated call (empty raw_jwt) cannot mint — fail-closed before the cache."""
    connector = _make_connector()
    with pytest.raises(VaultCredentialsReadError, match=r"vra-a"):
        await connector.auth_headers(_TARGET_A, operator=_make_operator(raw_jwt=""))
    await connector.aclose()


# ---------------------------------------------------------------------------
# Dispatch-path eviction hooks → re-mint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name", ["invalidate_session", "invalidate_credentials"])
@pytest.mark.asyncio
async def test_eviction_hook_drops_token_forcing_remint(hook_name: str) -> None:
    """Both dispatch-path eviction hooks drop the cached bearer so the next auth_headers re-mints.

    ``invalidate_session`` is the hook the dispatcher's data-path 401 recovery arm
    calls (#2067 — proven end-to-end in
    ``test_connectors_vra8_typed_reads::test_data_path_401_remints_via_dispatcher``);
    ``invalidate_credentials`` is the establish-failure companion (#2396). vRA 8
    keeps only the minted-bearer cache, so both evict it. This also pins that
    ``invalidate_session`` exists — its absence would be a latent permanent-wedge bug
    (the data-path 401 arm is keyed on ``invalidate_session``, not
    ``invalidate_credentials``).
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.post(_CSP_LOGIN_PATH).respond(200, json={"refresh_token": "rt"})
        iaas = mock.post(_IAAS_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"token": "bt-1"}),
                httpx.Response(200, json={"token": "bt-2"}),
            ]
        )
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        await getattr(connector, hook_name)(_TARGET_A)
        assert connector._session_tokens == {}
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == {"Authorization": "Bearer bt-1"}
    assert h2 == {"Authorization": "Bearer bt-2"}
    assert iaas.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_aclose_clears_tokens_and_pool() -> None:
    """aclose() drops the token cache and tears down the httpx pool."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        _mock_two_step_login(mock, bearer="bt")
        mock.get(_PROJECTS_PATH).respond(200, json={"content": []})
        await connector._vra_get(_TARGET_A, _PROJECTS_PATH, operator=_make_operator())

    assert connector._session_tokens == {target_cache_key(_TARGET_A): "bt"}
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._clients == {}


# ---------------------------------------------------------------------------
# fingerprint() + probe() — unauthenticated GET /iaas/api/about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reachable_no_mint() -> None:
    """A 2xx on /iaas/api/about → reachable, version=None, latest_api_version in extras, no mint."""
    connector = _make_connector()

    # assert_all_called=False: the login routes are registered to prove the probe
    # does NOT call them (an unauthenticated fingerprint must not mint a session).
    async with respx.mock(base_url="https://vra-a.test.invalid", assert_all_called=False) as mock:
        about = mock.get(_ABOUT_PATH).respond(
            200,
            json={
                "latestApiVersion": "2021-07-15",
                "supportedApis": [{"apiVersion": "2021-07-15"}, {"apiVersion": "2020-08-25"}],
            },
        )
        csp, iaas = _mock_two_step_login(mock)
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcfa"
    assert fp.reachable is True
    assert fp.version is None
    assert fp.extras["latest_api_version"] == "2021-07-15"
    assert fp.extras["supported_apis_count"] == 2
    assert fp.extras["api_surface"] == "vra-8"
    assert about.called
    # The probe is unauthenticated — it must NOT trigger a login.
    assert not csp.called
    assert not iaas.called
    assert connector._session_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_5xx() -> None:
    """A 5xx on /iaas/api/about → reachable=False with a structured error."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.get(_ABOUT_PATH).respond(503)
        fp = await connector.fingerprint(_TARGET_A)
    assert fp.reachable is False
    assert "HTTPStatusError" in fp.extras["error"] or "503" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_ok_and_not_ok() -> None:
    """probe() reflects fingerprint reachability."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vra-a.test.invalid") as mock:
        mock.get(_ABOUT_PATH).respond(200, json={"latestApiVersion": "2021-07-15"})
        ok = await connector.probe(_TARGET_A)
    assert ok.ok is True and ok.reason is None

    connector2 = _make_connector()
    async with respx.mock(base_url="https://vra-b.test.invalid") as mock:
        mock.get(_ABOUT_PATH).respond(503)
        bad = await connector2.probe(_TARGET_B)
    assert bad.ok is False and bad.reason is not None
    await connector.aclose()
    await connector2.aclose()


def test_default_loader_fails_closed_for_system_initiated_call() -> None:
    """The default Vault loader rejects an empty operator JWT (system-initiated call)."""
    from meho_backplane.connectors.vra8.session import load_credentials_from_vault

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match=r"vra-a"):
            await load_credentials_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    asyncio.run(_check())
