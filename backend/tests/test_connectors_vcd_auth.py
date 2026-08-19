# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VcloudDirectorConnector` provider session mint + fingerprint/probe.

Exercises the load-bearing vCD auth contract (the vCloud-Director-derived
Basic→Bearer flow the ``vcf-automation`` provider plane also speaks):

* ``POST /cloudapi/1.0.0/sessions/provider`` with HTTP Basic (``<user>@System``);
  the response header ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` is the JWT the connector
  caches and sends as ``Authorization: Bearer <jwt>`` on subsequent calls.
* Per-surface ``Accept``: the classic ``/api/*`` query service takes the
  versioned ``application/*+json`` form, the modern ``/cloudapi/*`` collections
  ``application/json`` — the one provider token authenticates both.
* Per-target token caching + tenant-unique isolation.
* ``invalidate_credentials`` eviction → re-mint (the dispatcher 401-recovery arm).
* ``GET /api/versions`` unauthenticated fingerprint/probe.
* ``auth_model`` gating + empty-``raw_jwt`` fail-closed.

Single-plane simplification of :mod:`tests.test_connectors_vcf_automation_auth`.
"""

from __future__ import annotations

import asyncio
import base64
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
from meho_backplane.connectors.vcd import (
    VCD_IMPL_ID,
    VCD_PRODUCT,
    VCD_VERSION,
    VcloudDirectorConnector,
    VcloudDirectorTargetLike,
)
from meho_backplane.settings import get_settings

_ORGS_PATH = "/cloudapi/1.0.0/orgs"
_QUERY_PATH = "/api/query"
_LOGIN_PATH = "/cloudapi/1.0.0/sessions/provider"
_TOKEN_HEADER = "X-VMWARE-VCLOUD-ACCESS-TOKEN"


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
def _clean_vcd_registry() -> Iterator[None]:
    """Restore VcloudDirectorConnector's package registration (both versioned + wildcard).

    Sibling tests clear the shared registry between tests; this fixture restores
    exactly what the ``vcd`` package's ``__init__`` registers — the versioned
    triple *and* the ``("vcd", "", "")`` wildcard — so every test sees the real
    registration shape.
    """
    clear_registry()
    for version, impl_id in (
        (VcloudDirectorConnector.version, VcloudDirectorConnector.impl_id),
        ("", ""),
    ):
        register_connector_v2(
            product=VcloudDirectorConnector.product,
            version=version,
            impl_id=impl_id,
            cls=VcloudDirectorConnector,
        )
    yield
    clear_registry()


@dataclass
class _StubTarget:
    """Target satisfying :data:`VcloudDirectorTargetLike` structurally."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(name="vcd-a", host="vcd-a.test.invalid", port=443, secret_ref="vcd/vcd-a")
_TARGET_B = _StubTarget(name="vcd-b", host="vcd-b.test.invalid", port=443, secret_ref="vcd/vcd-b")


async def _stub_loader(_target: VcloudDirectorTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target / operator."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VcloudDirectorConnector:
    """Build a connector wired with the stub loader."""
    return VcloudDirectorConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vcd_connector_subclasses_http_connector() -> None:
    """The connector inherits from HttpConnector with the right metadata."""
    assert issubclass(VcloudDirectorConnector, HttpConnector)
    assert VcloudDirectorConnector.product == "vcd"
    assert VcloudDirectorConnector.version == "10.6"
    assert VcloudDirectorConnector.impl_id == "vcd-rest"
    assert VcloudDirectorConnector.supported_version_range == ">=10.0,<11.0"
    assert VcloudDirectorConnector.priority == 1


def test_importing_package_registers_versioned_and_wildcard() -> None:
    """The vcd package registers BOTH the versioned triple and the ``("vcd","","")`` wildcard.

    The registration state is the package ``__init__``'s (restored verbatim by
    the autouse fixture); a net-new single-impl product owns its own wildcard, so
    an unfingerprinted vCD target still resolves.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    assert registry[(VCD_PRODUCT, VCD_VERSION, VCD_IMPL_ID)] is VcloudDirectorConnector
    assert registry[(VCD_PRODUCT, "", "")] is VcloudDirectorConnector


# ---------------------------------------------------------------------------
# Provider session mint + per-surface Accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloudapi_path_mints_bearer_and_sends_cloudapi_accept() -> None:
    """A ``/cloudapi/*`` GET mints the provider JWT and sends the cloudapi Accept."""
    connector = _make_connector()
    jwt = "provider-jwt-abc-123"

    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        login = mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: jwt})
        orgs = mock.get(_ORGS_PATH).respond(200, json={"values": []})
        result = await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())

    assert result == {"values": []}
    # Provider login used HTTP Basic with the <user>@System form.
    basic_user, basic_password = _decode_basic_auth(login.calls[0].request.headers["Authorization"])
    assert basic_user == "svc-meho@System"
    assert basic_password == "stub-password"
    # The data GET carried the Bearer + the cloudapi Accept.
    data_req = orgs.calls[0].request
    assert data_req.headers["Authorization"] == f"Bearer {jwt}"
    assert data_req.headers["Accept"] == "application/json;version=38.0"
    await connector.aclose()


@pytest.mark.asyncio
async def test_classic_api_path_sends_versioned_classic_accept() -> None:
    """A classic ``/api/*`` GET (query service) sends the versioned classic Accept."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "p-jwt"})
        query = mock.get(_QUERY_PATH).respond(200, json={"record": []})
        await connector._vcd_get(
            _TARGET_A,
            _QUERY_PATH,
            operator=_make_operator(),
            params={"type": "adminVM", "format": "records"},
        )

    req = query.calls[0].request
    assert req.headers["Accept"] == "application/*+json;version=38.0"
    assert req.headers["Authorization"] == "Bearer p-jwt"
    # The query-service selector rode as query params (not path structure).
    assert dict(req.url.params) == {"type": "adminVM", "format": "records"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_api_version_override_via_extras() -> None:
    """``extras['vcd_api_version']`` overrides the module default in the Accept."""
    target = _StubTarget(
        name="vcd-v37",
        host="vcd-v37.test.invalid",
        port=443,
        secret_ref="vcd/v37",
        extras={"vcd_api_version": "37.0"},
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://vcd-v37.test.invalid") as mock:
        # The login Accept also honours the override (cloudapi form).
        login = mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "p-jwt"})
        orgs = mock.get(_ORGS_PATH).respond(200, json={"values": []})
        await connector._vcd_get(target, _ORGS_PATH, operator=_make_operator())

    assert login.calls[0].request.headers["Accept"] == "application/json;version=37.0"
    assert orgs.calls[0].request.headers["Accept"] == "application/json;version=37.0"
    await connector.aclose()


@pytest.mark.asyncio
async def test_token_cached_across_calls_against_same_target() -> None:
    """Two calls against the same target mint the session once."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        login = mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "p-jwt"})
        mock.get(_ORGS_PATH).respond(200, json={"values": []})
        mock.get(_QUERY_PATH).respond(200, json={"record": []})
        await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())
        await connector._vcd_get(
            _TARGET_A, _QUERY_PATH, operator=_make_operator(), params={"type": "task"}
        )

    assert login.call_count == 1
    assert connector._session_tokens == {target_cache_key(_TARGET_A): "p-jwt"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_tokens_separate() -> None:
    """Two targets get two distinct cached tokens."""
    connector = _make_connector()

    async with respx.mock() as mock:
        mock.post("https://vcd-a.test.invalid" + _LOGIN_PATH).respond(
            200, headers={_TOKEN_HEADER: "p-a"}
        )
        mock.post("https://vcd-b.test.invalid" + _LOGIN_PATH).respond(
            200, headers={_TOKEN_HEADER: "p-b"}
        )
        mock.get("https://vcd-a.test.invalid" + _ORGS_PATH).respond(200, json={"values": []})
        mock.get("https://vcd-b.test.invalid" + _ORGS_PATH).respond(200, json={"values": []})
        await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())
        await connector._vcd_get(_TARGET_B, _ORGS_PATH, operator=_make_operator())

    assert connector._session_tokens == {
        target_cache_key(_TARGET_A): "p-a",
        target_cache_key(_TARGET_B): "p-b",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_tokens() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached token (#1642/#1672)."""
    connector = _make_connector()
    tenant_one = _StubTarget(
        name="vcd-shared",
        host="vcd-shared.test.invalid",
        port=443,
        secret_ref="vcd/shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="vcd-shared",
        host="vcd-shared.test.invalid",
        port=443,
        secret_ref="vcd/shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    async with respx.mock(base_url="https://vcd-shared.test.invalid") as mock:
        mock.post(_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, headers={_TOKEN_HEADER: "p-one"}),
                httpx.Response(200, headers={_TOKEN_HEADER: "p-two"}),
            ]
        )
        mock.get(_ORGS_PATH).respond(200, json={"values": []})
        await connector._vcd_get(tenant_one, _ORGS_PATH, operator=_make_operator())
        await connector._vcd_get(tenant_two, _ORGS_PATH, operator=_make_operator())

    assert connector._session_tokens == {
        target_cache_key(tenant_one): "p-one",
        target_cache_key(tenant_two): "p-two",
    }
    await connector.aclose()


# ---------------------------------------------------------------------------
# Login failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_401_surfaces_connector_auth_error_naming_target() -> None:
    """401 from provider login raises the structured ConnectorAuthError (#2329)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        mock.post(_LOGIN_PATH).respond(401)
        with pytest.raises(RuntimeError, match=r"vcd-a") as exc_info:
            await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())
    err = exc_info.value
    assert isinstance(err, ConnectorAuthError)
    assert "401" in str(err)
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_missing_token_header_raises() -> None:
    """A 2xx login with no X-VMWARE-VCLOUD-ACCESS-TOKEN raises naming the target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        mock.post(_LOGIN_PATH).respond(200)
        with pytest.raises(RuntimeError, match=r"vcd-a") as exc_info:
            await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())
    assert _TOKEN_HEADER in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_raises_clear_error() -> None:
    """A loader returning a dict without ``password`` raises naming the target."""

    async def _bad_loader(_t: VcloudDirectorTargetLike, _o: Operator) -> dict[str, str]:
        return {"username": "svc-meho"}  # missing 'password' on purpose

    connector = VcloudDirectorConnector(credentials_loader=_bad_loader)
    async with respx.mock(base_url="https://vcd-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"password"):
            await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())
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
        name="vcd-per-user",
        host="vcd.test.invalid",
        port=443,
        secret_ref="vcd/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())
    assert "vcd-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model() -> None:
    """``auth_model=None`` (pre-G0.3) is accepted."""
    target = _StubTarget(
        name="vcd-pre-g03", host="vcd.test.invalid", port=443, secret_ref="vcd/pre", auth_model=None
    )
    connector = _make_connector()
    async with respx.mock(base_url="https://vcd.test.invalid") as mock:
        mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "pre-jwt"})
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "Bearer pre-jwt"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_establish_rejects_empty_operator_jwt() -> None:
    """A system-initiated call (empty raw_jwt) cannot mint — fail-closed before the cache."""
    connector = _make_connector()
    with pytest.raises(VaultCredentialsReadError, match=r"vcd-a"):
        await connector.auth_headers(_TARGET_A, operator=_make_operator(raw_jwt=""))
    await connector.aclose()


# ---------------------------------------------------------------------------
# invalidate_credentials → re-mint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name", ["invalidate_session", "invalidate_credentials"])
@pytest.mark.asyncio
async def test_eviction_hook_drops_token_forcing_remint(hook_name: str) -> None:
    """Both dispatch-path eviction hooks drop the cached JWT so the next auth_headers re-mints.

    ``invalidate_session`` is the hook the dispatcher's data-path 401 recovery arm
    calls (#2067 — proven end-to-end in
    ``test_data_path_401_remints_via_dispatcher``); ``invalidate_credentials`` is
    the establish-failure companion (#2396). vCD keeps only the minted-JWT cache,
    so both evict it. This also pins that ``invalidate_session`` exists — its
    absence was a latent permanent-wedge bug (the data-path 401 arm is keyed on
    ``invalidate_session``, not ``invalidate_credentials``).
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        login = mock.post(_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, headers={_TOKEN_HEADER: "jwt-1"}),
                httpx.Response(200, headers={_TOKEN_HEADER: "jwt-2"}),
            ]
        )
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        await getattr(connector, hook_name)(_TARGET_A)
        assert connector._session_tokens == {}
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == {"Authorization": "Bearer jwt-1"}
    assert h2 == {"Authorization": "Bearer jwt-2"}
    assert login.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_aclose_clears_tokens_and_pool() -> None:
    """aclose() drops the token cache and tears down the httpx pool."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "jwt"})
        mock.get(_ORGS_PATH).respond(200, json={"values": []})
        await connector._vcd_get(_TARGET_A, _ORGS_PATH, operator=_make_operator())

    assert connector._session_tokens == {target_cache_key(_TARGET_A): "jwt"}
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._clients == {}


# ---------------------------------------------------------------------------
# fingerprint() + probe() — unauthenticated GET /api/versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reachable_no_mint() -> None:
    """A 2xx on /api/versions → reachable=True, version=None, and NO session mint."""
    connector = _make_connector()

    # assert_all_called=False: the login route is registered to prove the probe
    # does NOT call it (an unauthenticated fingerprint must not mint a session).
    async with respx.mock(base_url="https://vcd-a.test.invalid", assert_all_called=False) as mock:
        versions = mock.get("/api/versions").respond(
            200,
            content=b"<SupportedVersions><VersionInfo><Version>38.0</Version>"
            b"</VersionInfo></SupportedVersions>",
        )
        login = mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "should-not-mint"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcd"
    assert fp.reachable is True
    assert fp.version is None
    assert fp.extras["versions_status"] == 200
    assert fp.extras["api_surface"] == "vcloud-director"
    assert versions.called
    # The probe is unauthenticated — it must NOT trigger a provider login.
    assert not login.called
    assert connector._session_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_5xx() -> None:
    """A 5xx on /api/versions → reachable=False with a structured error."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        mock.get("/api/versions").respond(503)
        fp = await connector.fingerprint(_TARGET_A)
    assert fp.reachable is False
    assert "HTTPStatusError" in fp.extras["error"] or "503" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_ok_and_not_ok() -> None:
    """probe() reflects fingerprint reachability."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vcd-a.test.invalid") as mock:
        mock.get("/api/versions").respond(200, content=b"<x/>")
        ok = await connector.probe(_TARGET_A)
    assert ok.ok is True and ok.reason is None

    connector2 = _make_connector()
    async with respx.mock(base_url="https://vcd-b.test.invalid") as mock:
        mock.get("/api/versions").respond(503)
        bad = await connector2.probe(_TARGET_B)
    assert bad.ok is False and bad.reason is not None
    await connector.aclose()
    await connector2.aclose()


def test_default_loader_fails_closed_for_system_initiated_call() -> None:
    """The default Vault loader rejects an empty operator JWT (system-initiated call)."""
    from meho_backplane.connectors.vcd.session import load_credentials_from_vault

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match=r"vcd-a"):
            await load_credentials_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    asyncio.run(_check())
