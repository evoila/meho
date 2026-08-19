# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`SddcVcf5Connector` token-session mint + authenticated fingerprint.

Exercises the VCF 5.x SDDC Manager auth contract (identical to the modern 9.x flow):

* ``POST /v1/tokens`` with ``{username, password}`` returns ``{accessToken}``, cached
  and sent as ``Authorization: Bearer <accessToken>`` on every read.
* Per-target token caching + tenant-unique isolation.
* Login failure (non-2xx / token-less 2xx) → target-named ``SessionLoginError``.
* Both dispatch-path eviction hooks (``invalidate_session`` #2067 /
  ``invalidate_credentials`` #2396) → re-mint.
* ``auth_model`` gating + empty-``raw_jwt`` fail-closed.
* **Authenticated fingerprint** ``GET /v1/sddc-managers`` (SDDC Manager has no
  unauth version endpoint) — real version string, its 401 self-heal, and the
  operator-less fail-closed path.

Adapts :mod:`tests.test_connectors_vcd_auth` to the token (body) flow + auth fingerprint.
"""

from __future__ import annotations

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
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.sddc_manager.session import SddcTargetLike
from meho_backplane.connectors.sddc_vcf5 import (
    SDDC_VCF5_IMPL_ID,
    SDDC_VCF5_PRODUCT,
    SDDC_VCF5_VERSION,
    SddcVcf5Connector,
)
from meho_backplane.connectors.sddc_vcf5.connector import SessionLoginError
from meho_backplane.settings import get_settings

_TOKENS_PATH = "/v1/tokens"
_DOMAINS_PATH = "/v1/domains"
_MANAGERS_PATH = "/v1/sddc-managers"


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
def _clean_registry() -> Iterator[None]:
    """Restore SddcVcf5Connector's package registration (versioned triple only, no wildcard)."""
    clear_registry()
    register_connector_v2(
        product=SddcVcf5Connector.product,
        version=SddcVcf5Connector.version,
        impl_id=SddcVcf5Connector.impl_id,
        cls=SddcVcf5Connector,
    )
    yield
    clear_registry()


@dataclass
class _StubTarget:
    """Target satisfying :class:`SddcTargetLike` structurally."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(name="sddc-a", host="sddc-a.test.invalid", port=443, secret_ref="sddc/a")
_TARGET_B = _StubTarget(name="sddc-b", host="sddc-b.test.invalid", port=443, secret_ref="sddc/b")


async def _stub_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target / operator."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> SddcVcf5Connector:
    """Build a connector wired with the stub loader."""
    return SddcVcf5Connector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_sddc_vcf5_connector_subclasses_http_connector() -> None:
    """The connector inherits from HttpConnector with the legacy-dual-impl metadata."""
    assert issubclass(SddcVcf5Connector, HttpConnector)
    assert SddcVcf5Connector.product == "sddc"
    assert SddcVcf5Connector.version == "5.0"
    assert SddcVcf5Connector.impl_id == "sddc-vcf5"
    assert SddcVcf5Connector.supported_version_range == ">=5.0,<9.0"
    assert SddcVcf5Connector.priority == 1


def test_importing_package_registers_versioned_triple_only() -> None:
    """The package registers its versioned triple and NOT the ``("sddc","","")`` wildcard."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    assert registry[(SDDC_VCF5_PRODUCT, SDDC_VCF5_VERSION, SDDC_VCF5_IMPL_ID)] is SddcVcf5Connector
    assert (SDDC_VCF5_PRODUCT, "", "") not in registry


# ---------------------------------------------------------------------------
# Token-session mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_mints_token_and_sends_bearer() -> None:
    """A read mints the token via POST /v1/tokens and sends it as Bearer on the data GET."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        tokens = mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "tok-1"})
        domains = mock.get(_DOMAINS_PATH).respond(200, json={"elements": []})
        result = await connector._get_json(_TARGET_A, _DOMAINS_PATH, operator=_make_operator())

    assert result == {"elements": []}
    assert tokens.called
    data_req = domains.calls[0].request
    assert data_req.headers["Authorization"] == "Bearer tok-1"
    await connector.aclose()


@pytest.mark.asyncio
async def test_token_cached_across_calls() -> None:
    """Two reads against the same target mint the token once."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        tokens = mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "tok-c"})
        mock.get(_DOMAINS_PATH).respond(200, json={"elements": []})
        mock.get("/v1/clusters").respond(200, json={"elements": []})
        await connector._get_json(_TARGET_A, _DOMAINS_PATH, operator=_make_operator())
        await connector._get_json(_TARGET_A, "/v1/clusters", operator=_make_operator())

    assert tokens.call_count == 1
    assert connector._session_tokens == {target_cache_key(_TARGET_A): "tok-c"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_tokens() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached token (#1642)."""
    connector = _make_connector()
    tenant_one = _StubTarget(
        name="sddc-shared",
        host="sddc-shared.test.invalid",
        port=443,
        secret_ref="sddc/shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="sddc-shared",
        host="sddc-shared.test.invalid",
        port=443,
        secret_ref="sddc/shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    async with respx.mock(base_url="https://sddc-shared.test.invalid") as mock:
        mock.post(_TOKENS_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"accessToken": "tok-one"}),
                httpx.Response(200, json={"accessToken": "tok-two"}),
            ]
        )
        mock.get(_DOMAINS_PATH).respond(200, json={"elements": []})
        await connector._get_json(tenant_one, _DOMAINS_PATH, operator=_make_operator())
        await connector._get_json(tenant_two, _DOMAINS_PATH, operator=_make_operator())

    assert connector._session_tokens == {
        target_cache_key(tenant_one): "tok-one",
        target_cache_key(tenant_two): "tok-two",
    }
    await connector.aclose()


# ---------------------------------------------------------------------------
# Login failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_401_raises_session_login_error_naming_target() -> None:
    """401 from POST /v1/tokens raises the target-named SessionLoginError."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKENS_PATH).respond(401)
        with pytest.raises(SessionLoginError, match=r"sddc-a"):
            await connector._get_json(_TARGET_A, _DOMAINS_PATH, operator=_make_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_missing_access_token_raises_naming_target() -> None:
    """A 2xx token response with no accessToken raises the target-named SessionLoginError."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKENS_PATH).respond(200, json={"not_a_token": "x"})
        with pytest.raises(SessionLoginError, match=r"sddc-a"):
            await connector._get_json(_TARGET_A, _DOMAINS_PATH, operator=_make_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_raises_clear_error() -> None:
    """A loader returning a dict without ``password`` raises naming the target."""

    async def _bad_loader(_t: SddcTargetLike, _o: Operator) -> dict[str, str]:
        return {"username": "svc-meho"}  # missing 'password' on purpose

    connector = SddcVcf5Connector(credentials_loader=_bad_loader)
    async with respx.mock(base_url="https://sddc-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"password"):
            await connector._get_json(_TARGET_A, _DOMAINS_PATH, operator=_make_operator())
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
        name="sddc-per-user",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())
    assert "sddc-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model() -> None:
    """``auth_model=None`` (pre-G0.3) is accepted."""
    target = _StubTarget(
        name="sddc-pre", host="sddc.test.invalid", port=443, secret_ref="sddc/pre", auth_model=None
    )
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc.test.invalid") as mock:
        mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "pre-tok"})
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "Bearer pre-tok"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_establish_rejects_empty_operator_jwt() -> None:
    """A system-initiated call (empty raw_jwt) cannot mint — fail-closed before the cache."""
    connector = _make_connector()
    with pytest.raises(VaultCredentialsReadError, match=r"sddc-a"):
        await connector.auth_headers(_TARGET_A, operator=_make_operator(raw_jwt=""))
    await connector.aclose()


# ---------------------------------------------------------------------------
# Dispatch-path eviction hooks → re-mint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_name", ["invalidate_session", "invalidate_credentials"])
@pytest.mark.asyncio
async def test_eviction_hook_drops_token_forcing_remint(hook_name: str) -> None:
    """Both dispatch-path eviction hooks drop the cached token so the next auth_headers re-mints.

    ``invalidate_session`` is the data-path 401 arm (#2067 — proven end-to-end in the
    typed-reads test); ``invalidate_credentials`` is the establish-failure companion
    (#2396). This connector keeps only the token cache, so both evict it — pinning that
    ``invalidate_session`` exists (its absence would be a latent permanent-wedge bug).
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        tokens = mock.post(_TOKENS_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"accessToken": "tok-1"}),
                httpx.Response(200, json={"accessToken": "tok-2"}),
            ]
        )
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        await getattr(connector, hook_name)(_TARGET_A)
        assert connector._session_tokens == {}
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == {"Authorization": "Bearer tok-1"}
    assert h2 == {"Authorization": "Bearer tok-2"}
    assert tokens.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_aclose_clears_tokens_and_pool() -> None:
    """aclose() drops the token cache and tears down the httpx pool."""
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "tok"})
        mock.get(_DOMAINS_PATH).respond(200, json={"elements": []})
        await connector._get_json(_TARGET_A, _DOMAINS_PATH, operator=_make_operator())

    assert connector._session_tokens == {target_cache_key(_TARGET_A): "tok"}
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._clients == {}


# ---------------------------------------------------------------------------
# fingerprint() + probe() — authenticated GET /v1/sddc-managers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reads_real_version() -> None:
    """A 2xx on /v1/sddc-managers → reachable, version = the full VCF version string."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "fp-tok"})
        mock.get(_MANAGERS_PATH).respond(
            200,
            json={
                "elements": [
                    {
                        "id": "sddc-1",
                        "fqdn": "sddc-a.vcf.local",
                        "version": "5.2.0.0-24276214",
                        "domain": {"name": "MGMT"},
                    }
                ]
            },
        )
        fp = await connector.fingerprint(_TARGET_A, operator=_make_operator())

    assert fp.vendor == "vmware"
    assert fp.product == "sddc"
    assert fp.reachable is True
    assert fp.version == "5.2.0.0-24276214"
    assert fp.extras["management_domain"] == "MGMT"
    assert fp.extras["api_surface"] == "sddc-manager-vcf5"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_401_reretries_then_succeeds() -> None:
    """A 401 on the fingerprint GET self-heals via the internal session-retry helper."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        tokens = mock.post(_TOKENS_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"accessToken": "stale"}),
                httpx.Response(200, json={"accessToken": "fresh"}),
            ]
        )
        managers = mock.get(_MANAGERS_PATH).mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"elements": [{"version": "5.1.1.0-11111111"}]}),
            ]
        )
        fp = await connector.fingerprint(_TARGET_A, operator=_make_operator())

    assert fp.reachable is True
    assert fp.version == "5.1.1.0-11111111"
    assert tokens.call_count == 2
    assert managers.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_5xx() -> None:
    """A 5xx on /v1/sddc-managers → reachable=False with a structured error."""
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "tok"})
        mock.get(_MANAGERS_PATH).respond(503)
        fp = await connector.fingerprint(_TARGET_A, operator=_make_operator())
    assert fp.reachable is False
    assert "HTTPStatusError" in fp.extras["error"] or "503" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_operator_less_fails_closed_unreachable() -> None:
    """An operator-less fingerprint (no JWT) fails closed → unreachable, never authenticating."""
    connector = _make_connector()
    # No respx routes: the empty-JWT fail-closed trips before any HTTP call.
    fp = await connector.fingerprint(_TARGET_A)
    assert fp.reachable is False
    assert "VaultCredentialsReadError" in fp.extras["error"]
    assert connector._session_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_without_operator_is_not_ok() -> None:
    """``probe(target)`` takes no operator, so on the auth-only SDDC surface it fails closed.

    SDDC Manager has no unauthenticated endpoint, so ``probe(target)`` — which has no
    operator to authenticate with — delegates to ``fingerprint(target)`` (empty-JWT
    placeholder), fails closed, and reports not-ok. The real reachability check is
    ``fingerprint(target, operator)`` via the probe routes (tested above). This matches
    the modern ``sddc_manager`` sibling, whose probe likewise cannot authenticate
    without an operator. No respx routes: the fail-closed trips before any HTTP call.
    """
    connector = _make_connector()
    result = await connector.probe(_TARGET_A)
    assert result.ok is False
    # Pin the *fail-closed* reason specifically (not just any not-ok) so removing the
    # empty-JWT guard — which would let this fall through to a transport error — is
    # caught here too, not only by the two stronger operator-less tests above.
    assert result.reason is not None and "VaultCredentialsReadError" in result.reason
    assert connector._session_tokens == {}
    await connector.aclose()
