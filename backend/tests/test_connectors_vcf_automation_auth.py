# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VcfAutomationConnector` dual-plane auth + vhost + fingerprint/probe.

Exercises the load-bearing dual-plane contract:

* Provider plane: ``POST /cloudapi/1.0.0/sessions/provider`` with HTTP
  Basic; response header ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` is the JWT
  the connector caches and sends as ``Authorization: Bearer <jwt>`` on
  subsequent ``/cloudapi/*`` and ``/api/*`` calls.
* Tenant plane: ``POST /iaas/api/login`` with a JSON body; response
  body ``{"token": "..."}`` is the token the connector caches and
  sends as ``Authorization: Bearer <token>`` on subsequent
  ``/iaas/api/*`` calls.
* Plane selection by path prefix (``/iaas/api/*`` -> tenant, else
  provider).
* Per-plane 401 -> re-login + retry-once (independently per plane).
* Per-target isolation across both caches.
* Vhost routing: ``target.fqdn`` set -> base URL uses the FQDN; IP
  host with no ``fqdn`` -> :exc:`VcfAutomationConfigurationError`.
* ``auth_model != "shared_service_account"`` -> :exc:`NotImplementedError`.

The contract mirrors :mod:`tests.test_connectors_nsx_auth` and
:mod:`tests.test_connectors_sddc_manager_auth` with the dual-plane
divergence: two distinct login flows + two cached tokens per target
+ path-aware plane resolution.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
import respx

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_automation import (
    VcfAutomationConfigurationError,
    VcfAutomationConnector,
    VcfAutomationTargetLike,
)


@pytest.fixture(autouse=True)
def _clean_vcfa_registry() -> Iterator[None]:
    """Re-register VcfAutomationConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before
    every test in this module and clear after -- same pattern
    :mod:`tests.test_connectors_nsx_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=VcfAutomationConnector.product,
        version=VcfAutomationConnector.version,
        impl_id=VcfAutomationConnector.impl_id,
        cls=VcfAutomationConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub -- satisfies VcfAutomationTargetLike Protocol structurally.
# Replaced by the real Target model when fqdn / domain / provider_username /
# provider_secret_ref columns land in meho_backplane.targets.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    fqdn: str | None = None
    domain: str | None = None
    provider_username: str | None = None
    provider_secret_ref: str | None = None


_TARGET_A = _StubTarget(
    name="vcfa-a",
    host="vcfa-a.test.invalid",
    port=443,
    secret_ref="kv/data/vcfa/vcfa-a",
)
_TARGET_B = _StubTarget(
    name="vcfa-b",
    host="vcfa-b.test.invalid",
    port=443,
    secret_ref="kv/data/vcfa/vcfa-b",
)


async def _stub_loader(_target: VcfAutomationTargetLike) -> dict[str, str]:
    """Return canned credentials regardless of the target."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VcfAutomationConnector:
    """Build a connector wired with the stub loader."""
    return VcfAutomationConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vcfa_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(VcfAutomationConnector, HttpConnector)
    assert VcfAutomationConnector.product == "vcf-automation"
    assert VcfAutomationConnector.version == "9.0"
    assert VcfAutomationConnector.impl_id == "vcfa-rest"
    assert VcfAutomationConnector.supported_version_range == ">=9.0,<10.0"
    assert VcfAutomationConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vcf-automation", "9.0", "vcfa-rest")
    assert key in registry
    assert registry[key] is VcfAutomationConnector


def test_default_credentials_loader_raises_until_goal_214() -> None:
    """The default Vault loader stays unimplemented until Goal #214."""
    import asyncio

    from meho_backplane.connectors.vcf_automation.session import (
        load_credentials_from_vault,
    )

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"Goal #214"):
            await load_credentials_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Vhost routing -- base URL composition
# ---------------------------------------------------------------------------


def test_base_url_uses_fqdn_when_set() -> None:
    """``target.fqdn`` overrides ``target.host`` in the base URL."""
    target = _StubTarget(
        name="vcfa-vhost",
        host="10.0.0.5",
        port=443,
        secret_ref="kv/data/vcfa/vhost",
        fqdn="vcfa.canonical.invalid",
    )
    connector = _make_connector()
    assert connector._base_url(target) == "https://vcfa.canonical.invalid"


def test_base_url_uses_host_when_host_is_fqdn() -> None:
    """A target reached by FQDN without ``fqdn`` set works -- host already carries the vhost."""
    connector = _make_connector()
    assert connector._base_url(_TARGET_A) == "https://vcfa-a.test.invalid"


def test_base_url_with_non_default_port_is_appended() -> None:
    """Non-443 ports are appended to the base URL host."""
    target = _StubTarget(
        name="vcfa-port",
        host="vcfa-port.test.invalid",
        port=8443,
        secret_ref="kv/data/vcfa/port",
    )
    connector = _make_connector()
    assert connector._base_url(target) == "https://vcfa-port.test.invalid:8443"


@pytest.mark.parametrize("ip_host", ["10.0.0.5", "192.168.1.1", "::1", "[2001:db8::1]"])
def test_base_url_raises_configuration_error_when_ip_host_has_no_fqdn(ip_host: str) -> None:
    """An IP-literal host with no ``fqdn`` raises VcfAutomationConfigurationError.

    The consumer wrapper documents this as the silent-404 failure mode
    (VCFA returns 404 with empty body before the application sees the
    request when ``Host:`` doesn't match the appliance's expected
    vhost). The connector surfaces this at base-URL composition time
    so operators see a clear configuration message rather than a
    confusing 404 storm.
    """
    target = _StubTarget(
        name="vcfa-ip",
        host=ip_host,
        port=443,
        secret_ref="kv/data/vcfa/ip",
    )
    connector = _make_connector()
    with pytest.raises(VcfAutomationConfigurationError) as exc_info:
        connector._base_url(target)
    assert "vcfa-ip" in str(exc_info.value)
    assert ip_host in str(exc_info.value)
    assert "fqdn" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Plane selection by path prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_requires_path_argument() -> None:
    """auth_headers() without path= rejects -- dual-plane has no plane-agnostic header set."""
    connector = _make_connector()
    with pytest.raises(VcfAutomationConfigurationError, match=r"path"):
        await connector.auth_headers(_TARGET_A, raw_jwt="")
    await connector.aclose()


@pytest.mark.asyncio
async def test_provider_plane_path_returns_bearer_with_cloudapi_accept() -> None:
    """``/cloudapi/*`` path -> provider login + Bearer JWT + version=9.0.0 Accept."""
    connector = _make_connector()
    jwt = "provider-jwt-abc-123"

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200,
            headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": jwt},
        )
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")

    assert headers == {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json;version=9.0.0",
    }
    # Verify provider login used HTTP Basic with the default
    # ``<user>@System`` legacy username form (no provider_username,
    # no domain -> "System" default).
    request = login.calls[0].request
    auth_header = request.headers.get("Authorization", "")
    assert auth_header.startswith("Basic ")
    basic_user, basic_password = _decode_basic_auth(auth_header)
    assert basic_user == "svc-meho@System"
    assert basic_password == "stub-password"
    # Tenant login must NOT have fired for a provider-plane request.
    assert connector._tenant_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_provider_plane_api_path_uses_versioned_classic_accept() -> None:
    """``/api/*`` path -> provider plane but classic vCD Accept (#517)."""
    connector = _make_connector()
    jwt = "provider-jwt-xyz"

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": jwt}
        )
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/api/admin/extension")

    assert headers == {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/*+json;version=40.0",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_tenant_plane_path_returns_bearer_with_plain_accept() -> None:
    """``/iaas/api/*`` path -> tenant login + Bearer token + Accept: application/json."""
    connector = _make_connector()
    token = "tenant-token-456"

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        login = mock.post("/iaas/api/login").respond(200, json={"token": token})
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")

    assert headers == {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    # Verify tenant login used JSON body with the canonical credential
    # pair; ``domain`` is omitted when ``target.domain`` is unset.
    import json as _json

    request = login.calls[0].request
    assert request.headers.get("content-type", "").startswith("application/json")
    sent = _json.loads(request.content.decode())
    assert sent == {"username": "svc-meho", "password": "stub-password"}
    # Provider login must NOT have fired for a tenant-plane request.
    assert connector._provider_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_tenant_login_forwards_domain_when_target_has_domain() -> None:
    """``target.domain`` is forwarded on the tenant login JSON body."""
    target = _StubTarget(
        name="vcfa-domain",
        host="vcfa-domain.test.invalid",
        port=443,
        secret_ref="kv/data/vcfa/domain",
        domain="corp.example",
    )
    connector = _make_connector()
    token = "tenant-token-domain"

    async with respx.mock(base_url="https://vcfa-domain.test.invalid") as mock:
        login = mock.post("/iaas/api/login").respond(200, json={"token": token})
        await connector.auth_headers(target, raw_jwt="", path="/iaas/api/projects")

    import json as _json

    sent = _json.loads(login.calls[0].request.content.decode())
    assert sent == {
        "username": "svc-meho",
        "password": "stub-password",
        "domain": "corp.example",
    }
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-plane token caching + isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_token_cached_across_calls_against_same_target() -> None:
    """Two provider-plane auth_headers calls -> one provider login."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt"}
        )
        h1 = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
        h2 = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/regions")

    assert h1["Authorization"] == h2["Authorization"]
    assert login.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_tenant_token_cached_across_calls_against_same_target() -> None:
    """Two tenant-plane auth_headers calls -> one tenant login."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        login = mock.post("/iaas/api/login").respond(200, json={"token": "t-tok"})
        h1 = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")
        h2 = await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/deployments")

    assert h1["Authorization"] == h2["Authorization"]
    assert login.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_provider_and_tenant_caches_are_independent_per_target() -> None:
    """Establishing provider then tenant against the same target produces both tokens."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt-1"}
        )
        mock.post("/iaas/api/login").respond(200, json={"token": "t-tok-1"})
        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")

    assert connector._provider_tokens == {"vcfa-a": "p-jwt-1"}
    assert connector._tenant_tokens == {"vcfa-a": "t-tok-1"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_both_plane_caches_separate() -> None:
    """Two targets get two distinct token sets across both planes."""
    connector = _make_connector()

    async with respx.mock() as mock:
        mock.post("https://vcfa-a.test.invalid/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-a"}
        )
        mock.post("https://vcfa-b.test.invalid/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-b"}
        )
        mock.post("https://vcfa-a.test.invalid/iaas/api/login").respond(200, json={"token": "t-a"})
        mock.post("https://vcfa-b.test.invalid/iaas/api/login").respond(200, json={"token": "t-b"})

        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
        await connector.auth_headers(_TARGET_B, raw_jwt="", path="/cloudapi/1.0.0/orgs")
        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")
        await connector.auth_headers(_TARGET_B, raw_jwt="", path="/iaas/api/projects")

    assert connector._provider_tokens == {"vcfa-a": "p-a", "vcfa-b": "p-b"}
    assert connector._tenant_tokens == {"vcfa-a": "t-a", "vcfa-b": "t-b"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# Provider username override (admin@System vs svc-meho)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_username_override_is_used_verbatim() -> None:
    """``target.provider_username`` overrides the default ``<user>@System`` form."""
    target = _StubTarget(
        name="vcfa-prov-user",
        host="vcfa-prov-user.test.invalid",
        port=443,
        secret_ref="kv/data/vcfa/prov-user",
        provider_username="admin@System",
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-prov-user.test.invalid") as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt"}
        )
        await connector.auth_headers(target, raw_jwt="", path="/cloudapi/1.0.0/orgs")

    auth = login.calls[0].request.headers["Authorization"]
    user, password = _decode_basic_auth(auth)
    assert user == "admin@System"
    assert password == "stub-password"
    await connector.aclose()


@pytest.mark.asyncio
async def test_provider_secret_ref_override_invokes_loader_for_distinct_provider_pair() -> None:
    """``target.provider_secret_ref`` -> loader is called twice (provider + tenant secret refs).

    The override path lets operators store distinct provider-plane
    credentials (e.g. the VCFA-local ``admin@System`` account
    password) at a different Vault path than the SSO/tenant secret.
    """
    target = _StubTarget(
        name="vcfa-prov-secret",
        host="vcfa-prov-secret.test.invalid",
        port=443,
        secret_ref="kv/data/vcfa/sso",
        provider_secret_ref="kv/data/vcfa/provider",
        provider_username="admin@System",
    )

    seen_secret_refs: list[str] = []

    async def _ref_tracking_loader(t: VcfAutomationTargetLike) -> dict[str, str]:
        seen_secret_refs.append(t.secret_ref)
        if t.secret_ref == "kv/data/vcfa/provider":
            return {"username": "admin", "password": "provider-secret"}
        return {"username": "svc-meho", "password": "tenant-secret"}

    connector = VcfAutomationConnector(credentials_loader=_ref_tracking_loader)

    async with respx.mock(base_url="https://vcfa-prov-secret.test.invalid") as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt"}
        )
        await connector.auth_headers(target, raw_jwt="", path="/cloudapi/1.0.0/orgs")

    # Provider login carried the *provider-only* password.
    auth = login.calls[0].request.headers["Authorization"]
    _, password = _decode_basic_auth(auth)
    assert password == "provider-secret"
    assert "kv/data/vcfa/provider" in seen_secret_refs
    await connector.aclose()


# ---------------------------------------------------------------------------
# Failure modes -- login errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_login_401_surfaces_runtime_error_naming_target() -> None:
    """401 from provider login raises RuntimeError naming the target + status."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(401)
        with pytest.raises(RuntimeError, match=r"vcfa-a") as exc_info:
            await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
    assert "401" in str(exc_info.value)
    assert "provider" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_provider_login_missing_token_header_raises() -> None:
    """2xx with no X-VMWARE-VCLOUD-ACCESS-TOKEN raises naming the target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(200)
        with pytest.raises(RuntimeError, match=r"vcfa-a") as exc_info:
            await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
    assert "X-VMWARE-VCLOUD-ACCESS-TOKEN" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_tenant_login_401_surfaces_runtime_error_naming_target() -> None:
    """401 from tenant login raises RuntimeError naming the target + plane."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/iaas/api/login").respond(401)
        with pytest.raises(RuntimeError, match=r"vcfa-a") as exc_info:
            await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")
    assert "401" in str(exc_info.value)
    assert "tenant" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_tenant_login_empty_token_field_raises() -> None:
    """2xx tenant login with no ``token`` field raises naming the target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/iaas/api/login").respond(200, json={"sessionId": "wrong-shape"})
        with pytest.raises(RuntimeError, match=r"vcfa-a") as exc_info:
            await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")
    assert "token" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_raises_clear_error() -> None:
    """A loader returning a dict without ``password`` raises naming the target."""

    async def _bad_loader(_t: VcfAutomationTargetLike) -> dict[str, str]:
        return {"username": "svc-meho"}  # type: ignore[return-value]

    connector = VcfAutomationConnector(credentials_loader=_bad_loader)

    async with respx.mock(base_url="https://vcfa-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"password"):
            await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation / unknown modes raise NotImplementedError naming target + mode."""
    target = _StubTarget(
        name="vcfa-per-user",
        host="vcfa.test.invalid",
        port=443,
        secret_ref="kv/data/vcfa/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, raw_jwt="", path="/cloudapi/1.0.0/orgs")
    assert "vcfa-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """``auth_model=None`` is accepted (pre-G0.3 column-not-yet-populated)."""
    target = _StubTarget(
        name="vcfa-pre-g03",
        host="vcfa.test.invalid",
        port=443,
        secret_ref="kv/data/vcfa/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "pre-g03-jwt"}
        )
        headers = await connector.auth_headers(target, raw_jwt="", path="/cloudapi/1.0.0/orgs")
    assert headers["Authorization"] == "Bearer pre-g03-jwt"
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An :class:`AuthModel` enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="vcfa-enum",
        host="vcfa.test.invalid",
        port=443,
        secret_ref="kv/data/vcfa/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "enum-jwt"}
        )
        headers = await connector.auth_headers(target, raw_jwt="", path="/cloudapi/1.0.0/orgs")
    assert headers["Authorization"] == "Bearer enum-jwt"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-plane 401 -> re-login + retry-once recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_plane_401_triggers_relogin_and_retry_once() -> None:
    """A 401 on a ``/cloudapi/*`` call triggers provider re-login + a single retry."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider")
        login.side_effect = [
            httpx.Response(200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt-first"}),
            httpx.Response(200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt-second"}),
        ]
        orgs = mock.get("/cloudapi/1.0.0/orgs")
        orgs.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"values": [{"name": "System"}]}),
        ]

        result = await connector._request_json(_TARGET_A, "GET", "/cloudapi/1.0.0/orgs", raw_jwt="")

    assert result == {"values": [{"name": "System"}]}
    assert login.call_count == 2
    assert orgs.call_count == 2
    # Cache holds the refreshed token; tenant cache untouched.
    assert connector._provider_tokens == {"vcfa-a": "p-jwt-second"}
    assert connector._tenant_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_provider_plane_401_then_401_after_relogin_raises_runtime_error() -> None:
    """A repeated 401 (post-relogin) surfaces as RuntimeError naming the target + plane."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt"}
        )
        orgs = mock.get("/cloudapi/1.0.0/orgs").respond(401)

        with pytest.raises(RuntimeError, match=r"vcfa-a") as exc_info:
            await connector._request_json(_TARGET_A, "GET", "/cloudapi/1.0.0/orgs", raw_jwt="")

    assert "after refresh" in str(exc_info.value)
    assert "provider" in str(exc_info.value)
    assert login.call_count == 2
    assert orgs.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_tenant_plane_401_triggers_independent_relogin_and_retry_once() -> None:
    """A 401 on ``/iaas/api/*`` re-logs the tenant plane only -- provider cache untouched."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        # Seed a provider token first so we can assert it survives.
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt-stable"}
        )
        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")

        tenant_login = mock.post("/iaas/api/login")
        tenant_login.side_effect = [
            httpx.Response(200, json={"token": "t-first"}),
            httpx.Response(200, json={"token": "t-second"}),
        ]
        projects = mock.get("/iaas/api/projects")
        projects.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"content": [{"name": "default"}]}),
        ]

        result = await connector._request_json(_TARGET_A, "GET", "/iaas/api/projects", raw_jwt="")

    assert result == {"content": [{"name": "default"}]}
    assert tenant_login.call_count == 2
    assert projects.call_count == 2
    # The provider cache survived the tenant-plane re-login.
    assert connector._provider_tokens == {"vcfa-a": "p-jwt-stable"}
    assert connector._tenant_tokens == {"vcfa-a": "t-second"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_request_json_rejects_non_idempotent_method() -> None:
    """_request_json mirrors the base ABC: non-idempotent verbs raise ValueError."""
    connector = _make_connector()
    with pytest.raises(ValueError, match=r"idempotent"):
        await connector._request_json(_TARGET_A, "POST", "/cloudapi/1.0.0/orgs", raw_jwt="")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() + probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_when_both_planes_reachable() -> None:
    """Both unauthenticated probes succeed -> reachable=True + canonical extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.get("/api/versions").respond(
            200,
            content=b"<SupportedVersions><VersionInfo deprecated='false'>"
            b"<Version>9.0.0</Version></VersionInfo></SupportedVersions>",
            content_type="application/xml",
        )
        mock.get("/iaas/api/about").respond(
            200,
            json={
                "latestApiVersion": "2024-02-20",
                "supportedApis": ["iaas", "lcm"],
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-automation"
    assert fp.reachable is True
    assert fp.version == "2024-02-20"
    assert fp.probe_method == "GET /api/versions + GET /iaas/api/about"
    assert fp.extras["planes"] == ["provider", "tenant"]
    assert fp.extras["provider_versions_status"] == 200
    assert fp.extras["tenant_supported_apis"] == ["iaas", "lcm"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_provider_failure_returns_reachable_false() -> None:
    """A 5xx on the provider probe surfaces as reachable=False naming the failed plane."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid", assert_all_called=False) as mock:
        mock.get("/api/versions").respond(503)
        mock.get("/iaas/api/about").respond(200, json={"latestApiVersion": "x"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is False
    assert fp.extras["failed_plane"] == "provider"
    assert "HTTPStatusError" in fp.extras["error"] or "503" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_tenant_failure_returns_reachable_false() -> None:
    """A 5xx on the tenant probe surfaces as reachable=False naming the failed plane."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.get("/api/versions").respond(200, content=b"<x/>")
        mock.get("/iaas/api/about").respond(503)
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is False
    assert fp.extras["failed_plane"] == "tenant"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_ip_host_without_fqdn_surfaces_configuration_error() -> None:
    """Vhost mis-config (IP host, no fqdn) -> reachable=False with structured error."""
    target = _StubTarget(
        name="vcfa-ip-fp",
        host="10.0.0.5",
        port=443,
        secret_ref="kv/data/vcfa/ip-fp",
    )
    connector = _make_connector()

    fp = await connector.fingerprint(target)
    assert fp.reachable is False
    error = fp.extras["error"]
    assert "VcfAutomationConfigurationError" in error
    assert "fqdn" in error.lower()
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_both_planes_reachable() -> None:
    """probe() returns ok=True when fingerprint succeeds across both planes."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.get("/api/versions").respond(200, content=b"<x/>")
        mock.get("/iaas/api/about").respond(200, json={"latestApiVersion": "x"})
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_when_a_plane_fails() -> None:
    """probe() returns ok=False + reason on either plane failure."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid", assert_all_called=False) as mock:
        mock.get("/api/versions").respond(503)
        mock.get("/iaas/api/about").respond(200, json={})
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose -- clears both plane caches + tears down pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_both_plane_caches_and_pool() -> None:
    """aclose() drops both provider+tenant token caches and tears down the httpx pool."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcfa-a.test.invalid") as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": "p-jwt"}
        )
        mock.post("/iaas/api/login").respond(200, json={"token": "t-tok"})
        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/cloudapi/1.0.0/orgs")
        await connector.auth_headers(_TARGET_A, raw_jwt="", path="/iaas/api/projects")

    assert connector._provider_tokens == {"vcfa-a": "p-jwt"}
    assert connector._tenant_tokens == {"vcfa-a": "t-tok"}
    await connector.aclose()
    assert connector._provider_tokens == {}
    assert connector._tenant_tokens == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_sessions_is_a_noop() -> None:
    """A fresh connector with no sessions established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._provider_tokens == {}
    assert connector._tenant_tokens == {}
