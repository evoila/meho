# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VmwareRestConnector` session-auth + lifecycle (G3.1-T1 #498).

The integration shape (real vcsim) lives in
:mod:`tests.integration.test_connectors_vmware_rest_vcsim`. This module
exercises the same contract with respx-mocked HTTP so the gate runs in
every CI lane regardless of Docker availability.

Coverage matrix (per #498 acceptance criteria):

* Session establishment success — ``POST /api/session`` returns a
  JSON-string token (vSphere 7.0+ shape); the token is cached under
  ``target.name`` and returned in the
  ``vmware-api-session-id`` header on subsequent calls.
* Session reuse — a second auth call against the same target does NOT
  re-issue ``POST /api/session``.
* Session-creation failure — 401 from ``/api/session`` surfaces as a
  ``RuntimeError`` whose message names the target.
* Per-target isolation — two distinct ``target.name`` values get two
  distinct cached tokens; cross-target token leakage would be a tenant-
  isolation bug.
* :meth:`aclose` revokes every cached session via ``DELETE /api/session``
  before invoking :meth:`HttpConnector.aclose`.
* :meth:`auth_headers` raises :exc:`NotImplementedError` when
  ``target.auth_model`` is ``"per_user"`` or ``"impersonation"``.
* Loader-returning-incomplete-dict surfaces a clear error.
* Legacy ``{"value": "<token>"}`` response shape is accepted defensively
  for cross-vcsim-version compatibility.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface.

    Defaults ``raw_jwt`` to a non-empty placeholder so the cache fast-path's
    defense-in-depth fail-closed guard (``VaultCredentialsReadError`` on
    empty ``operator.raw_jwt``, mirroring the loader-path guard) doesn't
    fire on tests that don't care about the value. Tests that exercise the
    empty-jwt rejection pass ``raw_jwt=""`` explicitly.
    """
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``Settings`` reads at construction time.

    The live default loader's ``load_basic_credentials`` →
    ``vault_client_for_operator`` calls ``get_settings()`` which eagerly
    reads ``KEYCLOAK_*`` / ``VAULT_*``. The respx-only tests in this
    module never reach Vault (they inject a stub loader), but the
    live-read test below does, so pin the env for the whole module — the
    same shape ``test_connectors_vault_creds.py`` uses.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_vmware_rest_registry() -> Iterator[None]:
    """Re-register VmwareRestConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` (and other earlier-alphabetised
    test modules) install autouse fixtures that call
    :func:`clear_registry` between tests. The connector class
    self-registered at import time, but the post-clear empty state
    breaks the registration-acceptance test below. Re-register before
    every test in this module and clear after — same pattern
    :mod:`tests.test_connectors_vault` established.
    """
    clear_registry()
    register_connector_v2(
        product=VmwareRestConnector.product,
        version=VmwareRestConnector.version,
        impl_id=VmwareRestConnector.impl_id,
        cls=VmwareRestConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies VsphereTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None  # #2398: per-target TLS SNI / cert-verify name
    # Tenant-unique cache key components (#1642/#1672). Distinct ``id`` per
    # instance so two stub targets never collapse onto one cache entry.
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="vcenter-a",
    host="vcenter-a.test.invalid",
    port=443,
    secret_ref="vsphere/vcenter-a",
)
_TARGET_B = _StubTarget(
    name="vcenter-b",
    host="vcenter-b.test.invalid",
    port=443,
    secret_ref="vsphere/vcenter-b",
)


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target/operator."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    """Build a connector wired with the stub loader."""
    return VmwareRestConnector(session_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vmware_rest_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector."""
    assert issubclass(VmwareRestConnector, HttpConnector)
    assert VmwareRestConnector.product == "vmware"
    assert VmwareRestConnector.version == "9.0"
    assert VmwareRestConnector.impl_id == "vmware-rest"
    assert VmwareRestConnector.supported_version_range == ">=8.5,<10.0"
    # Outranks GenericRestConnector auto-shim (priority=0) defensively
    # if both somehow register for the same triple.
    assert VmwareRestConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vmware", "9.0", "vmware-rest")
    assert key in registry
    assert registry[key] is VmwareRestConnector


@pytest.mark.asyncio
async def test_default_session_loader_does_live_vault_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default loader reads ``{username, password}`` from Vault, JWT-forwarded.

    Replaces the former ``raises deliberate stub`` test: with G3.9-T2's
    shared helper wired, the default loader performs the live KV-v2 read
    under the operator's identity (rubric State 2). The in-process Vault
    fake (``install_fake_client``) stands in for hvac so the unit lane
    stays secret-free; the live Vault round-trip is the env-gated lab
    smoke in ``tests/integration``.
    """
    from meho_backplane.connectors.vmware_rest.session import (
        load_session_credentials_from_vault,
    )

    fake = install_fake_client(
        monkeypatch,
        secret={"username": "svc-meho", "password": "vault-read-pw"},
    )

    creds = await load_session_credentials_from_vault(_TARGET_A, _make_operator("op.jwt"))

    assert creds == {"username": "svc-meho", "password": "vault-read-pw"}
    # The operator's JWT was forwarded to Vault's JWT/OIDC login.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.jwt"
    # The read addressed the target's secret_ref under the default mount.
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == _TARGET_A.secret_ref


@pytest.mark.asyncio
async def test_default_session_loader_fails_closed_for_system_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system-initiated call (empty raw_jwt) errors before touching Vault.

    The shared helper's fail-closed carve-out: no operator JWT means no
    operator-context read. Confirms the vmware default loader inherits
    that contract rather than silently falling back.
    """
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.vmware_rest.session import (
        load_session_credentials_from_vault,
    )

    fake = install_fake_client(monkeypatch)

    with pytest.raises(VaultCredentialsReadError):
        await load_session_credentials_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    # Vault was never reached — no login, no read.
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []


# ---------------------------------------------------------------------------
# Session establishment — happy path
# ---------------------------------------------------------------------------


# The connector's aclose() issues DELETE /api/session for every cached
# target; tests that don't mock that endpoint would tip respx into an
# AssertionError on the unhandled request. ``_patch_no_revoke_aclose``
# replaces the cleanup with a direct httpx pool tear-down for tests
# that don't care about the revoke leg — kept here at module scope so
# every test that follows can call it after building its connector.
def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Replace connector.aclose with a revoke-free pool tear-down."""

    async def _aclose() -> None:
        connector._session_tokens.clear()
        connector._session_paths.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_auth_headers_establishes_session_and_returns_header() -> None:
    """First auth_headers call POSTs to /api/session and caches the token."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    token = "abc-session-token-123"

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        session_route = mock.post("/api/session").respond(200, json=token)
        headers = await connector.auth_headers(_TARGET_A, _make_operator())

    assert session_route.called
    assert session_route.call_count == 1
    assert headers == {"vmware-api-session-id": token}
    # Basic auth from the stub loader: svc-meho / stub-password.
    sent_auth = session_route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None
    assert sent_auth.startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_session_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-POST /api/session."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    token = "cached-token-456"

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        session_route = mock.post("/api/session").respond(200, json=token)
        h1 = await connector.auth_headers(_TARGET_A, _make_operator())
        h2 = await connector.auth_headers(_TARGET_A, _make_operator())

    assert h1 == h2 == {"vmware-api-session-id": token}
    # The load-bearing assertion — exactly one POST /api/session for two
    # auth header calls.
    assert session_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_session_tokens_separate() -> None:
    """Two targets get two distinct cached tokens; no cross-target leakage."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock() as mock:
        route_a = mock.post("https://vcenter-a.test.invalid/api/session").respond(
            200, json="token-for-a"
        )
        route_b = mock.post("https://vcenter-b.test.invalid/api/session").respond(
            200, json="token-for-b"
        )

        h_a = await connector.auth_headers(_TARGET_A, _make_operator())
        h_b = await connector.auth_headers(_TARGET_B, _make_operator())

    assert route_a.called and route_b.called
    assert h_a == {"vmware-api-session-id": "token-for-a"}
    assert h_b == {"vmware-api-session-id": "token-for-b"}
    # Both tokens cached.
    assert connector._session_tokens == {
        target_cache_key(_TARGET_A): "token-for-a",
        target_cache_key(_TARGET_B): "token-for-b",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_invalidate_session_evicts_only_the_targets_slot() -> None:
    """``invalidate_session`` drops one target's token + path, leaving others.

    G0.29-T2 (#2067): the dispatch-path recovery hook. Seeds two targets'
    cached tokens + login paths, invalidates target A, and asserts only A's
    slot is evicted -- per-``(tenant_id, target.id)`` isolation holds across
    eviction (#1642/#1672/#1684), so a re-login for A never disturbs B.
    """
    connector = _make_connector()
    key_a = target_cache_key(_TARGET_A)
    key_b = target_cache_key(_TARGET_B)
    connector._session_tokens[key_a] = "token-for-a"
    connector._session_tokens[key_b] = "token-for-b"
    connector._session_paths[key_a] = "/api/session"
    connector._session_paths[key_b] = "/rest/com/vmware/cis/session"

    await connector.invalidate_session(_TARGET_A)

    # A's token + path are gone; B's are untouched.
    assert connector._session_tokens == {key_b: "token-for-b"}
    assert connector._session_paths == {key_b: "/rest/com/vmware/cis/session"}

    # A second invalidation of an already-evicted target is a no-op.
    await connector.invalidate_session(_TARGET_A)
    assert connector._session_tokens == {key_b: "token-for-b"}


# ---------------------------------------------------------------------------
# adapt_op_query — mount-flavor filter-param keying (#2298)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapt_op_query_strips_filter_prefix_on_modern_mount() -> None:
    """On a modern ``/api`` session the ``filter.`` prefix is stripped (#2298).

    Real vCenter 8.x 400s ``filter.datastores`` and wants the bare
    ``datastores``; the seam keys the param style off the established mount.
    """
    connector = _make_connector()
    key = target_cache_key(_TARGET_A)
    connector._session_tokens[key] = "tok"
    connector._session_paths[key] = "/api/session"

    adapted = await connector.adapt_op_query(
        _TARGET_A, {"filter.datastores": ["ds-1"], "filter.names": ["n"]}, _make_operator()
    )

    assert adapted == {"datastores": ["ds-1"], "names": ["n"]}


@pytest.mark.asyncio
async def test_adapt_op_query_keeps_filter_prefix_on_legacy_mount() -> None:
    """On a legacy ``/rest`` session (vcsim) the ``filter.`` prefix is kept (#2298)."""
    connector = _make_connector()
    key = target_cache_key(_TARGET_A)
    connector._session_tokens[key] = "tok"
    connector._session_paths[key] = "/rest/com/vmware/cis/session"

    adapted = await connector.adapt_op_query(
        _TARGET_A, {"filter.hosts": ["host-1"]}, _make_operator()
    )

    assert adapted == {"filter.hosts": ["host-1"]}


@pytest.mark.asyncio
async def test_adapt_op_query_empty_short_circuits_to_none() -> None:
    """An empty / ``None`` query returns ``None`` without a session establish (#2298)."""
    connector = _make_connector()

    assert await connector.adapt_op_query(_TARGET_A, {}, _make_operator()) is None
    assert await connector.adapt_op_query(_TARGET_A, None, _make_operator()) is None
    # No session was established for the short-circuit path.
    assert connector._session_tokens == {}


@pytest.mark.asyncio
async def test_invalidate_then_auth_headers_re_establishes_session() -> None:
    """After ``invalidate_session``, the next ``auth_headers`` re-logs-in.

    Proves the eviction forces a cache miss -> ``_establish_and_cache_session``
    (a fresh ``POST /api/session``), the recovery the dispatcher relies on for
    vCenter's cold-401 -- no backplane restart.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock() as mock:
        route = mock.post("https://vcenter-a.test.invalid/api/session").respond(
            200, json="fresh-token"
        )
        connector._session_tokens[target_cache_key(_TARGET_A)] = "stale-token"

        await connector.invalidate_session(_TARGET_A)
        headers = await connector.auth_headers(_TARGET_A, _make_operator())

    assert route.called  # re-login actually fired
    assert headers == {"vmware-api-session-id": "fresh-token"}
    assert connector._session_tokens[target_cache_key(_TARGET_A)] == "fresh-token"
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_sessions() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached session.

    Regression guard for #1642/#1672: the session-token cache used to key
    on ``target.name`` alone, so two same-named targets in different
    tenants collapsed onto one entry and one tenant could be served
    another tenant's session. The cache keys on the tenant-unique
    ``(tenant_id, id)`` tuple instead. Both stub targets share one host
    so the established session token, not the per-target HTTP-client pool,
    is the variable under test.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    tenant_one = _StubTarget(
        name="vcenter-shared",
        host="vcenter-shared.test.invalid",
        port=443,
        secret_ref="vsphere/vcenter-shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="vcenter-shared",
        host="vcenter-shared.test.invalid",
        port=443,
        secret_ref="vsphere/vcenter-shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    async with respx.mock(base_url="https://vcenter-shared.test.invalid") as mock:
        route = mock.post("/api/session").mock(
            side_effect=[
                httpx.Response(200, json="token-tenant-one"),
                httpx.Response(200, json="token-tenant-two"),
            ]
        )
        h_one = await connector.auth_headers(tenant_one, _make_operator())
        h_two = await connector.auth_headers(tenant_two, _make_operator())

    # Each tenant established its own session — no cross-tenant cache hit.
    assert route.call_count == 2
    assert h_one == {"vmware-api-session-id": "token-tenant-one"}
    assert h_two == {"vmware-api-session-id": "token-tenant-two"}
    assert connector._session_tokens == {
        target_cache_key(tenant_one): "token-tenant-one",
        target_cache_key(tenant_two): "token-tenant-two",
    }

    # Same-tenant re-fetch is a cache HIT — behaviour unchanged.
    h_one_again = await connector.auth_headers(tenant_one, _make_operator())
    assert h_one_again == {"vmware-api-session-id": "token-tenant-one"}
    assert route.call_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Session establishment — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_create_401_surfaces_connector_auth_error_with_target_name() -> None:
    """401 from POST /api/session raises the structured ConnectorAuthError (#2329).

    Still a ``RuntimeError`` subclass (via ``SessionLoginError``) so pre-#2329
    callers are unaffected, but now carries the establish cause sub-code the
    dispatcher maps to ``connector_auth_failed``.
    """
    from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError

    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(401, json={"error": "invalid_credentials"})
        with pytest.raises(RuntimeError, match=r"vcenter-a") as exc_info:
            await connector.auth_headers(_TARGET_A, _make_operator())

    err = exc_info.value
    assert isinstance(err, ConnectorAuthError)
    assert err.cause == "session_establish_401"
    assert err.status_code == 401
    # The wrapped HTTPStatusError carries the 401 details.
    assert "401" in str(err)
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_response_unexpected_shape_raises() -> None:
    """A response that isn't a JSON string or {"value": str} raises."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        # POST /api/session returns something the connector can't parse —
        # an empty object is the most common vcsim misbehaviour shape.
        mock.post("/api/session").respond(200, json={"unrelated": "field"})
        with pytest.raises(RuntimeError, match=r"vcenter-a"):
            await connector.auth_headers(_TARGET_A, _make_operator())

    await connector.aclose()


@pytest.mark.asyncio
async def test_session_legacy_object_shape_is_accepted_defensively() -> None:
    """Pre-7.0 ``{"value": "<token>"}`` response is still parsed correctly.

    Some vcsim builds straddle the modern-string and legacy-object
    shapes; the connector accepts both so the integration test stays
    green across simulator versions.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(200, json={"value": "legacy-shape-token"})
        headers = await connector.auth_headers(_TARGET_A, _make_operator())

    assert headers == {"vmware-api-session-id": "legacy-shape-token"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# Modern → legacy session-endpoint fallback
# ---------------------------------------------------------------------------
#
# Real vCenter (8.5+) serves both ``POST /api/session`` and the legacy
# ``POST /rest/com/vmware/cis/session``; the upstream ``vmware/vcsim``
# simulator wires only the legacy path (see
# ``govmomi/vapi/simulator/simulator.go``, which registers handlers at
# ``rest.Path + internal.SessionPath`` = ``/rest`` + ``/com/vmware/cis/session``
# without a parallel ``/api/`` mount). The connector POSTs to the modern
# path first and, on HTTP 404 only, retries against the legacy path so
# production hits the fast path while the vcsim integration test stays
# green across simulator versions. The path that succeeded is cached
# per-target so :meth:`aclose` DELETEs against the same endpoint that
# minted the token.


@pytest.mark.asyncio
async def test_modern_session_endpoint_is_tried_first_no_fallback_on_200() -> None:
    """A 200 from /api/session means we never touch the legacy path.

    The legacy route is *deliberately not registered* — if the connector
    erroneously fell back to it, respx would raise an unhandled-request
    error and fail the test. That's a stricter signal than asserting
    ``not legacy.called`` on a pre-registered route (which respx itself
    would also flag via ``assert_all_called``).
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        modern = mock.post("/api/session").respond(200, json="modern-path-token")
        headers = await connector.auth_headers(_TARGET_A, _make_operator())

    assert headers == {"vmware-api-session-id": "modern-path-token"}
    assert modern.called and modern.call_count == 1
    # The cached path records the modern endpoint so aclose targets it.
    assert connector._session_paths == {target_cache_key(_TARGET_A): "/api/session"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_falls_back_to_legacy_path_on_modern_404() -> None:
    """A 404 from /api/session triggers a retry against /rest/com/vmware/cis/session.

    This is the vcsim path: stock vcsim registers the session handler
    only at the legacy endpoint per govmomi/vapi/simulator/simulator.go.
    Production vCenter serves both paths so the fallback is dormant there.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        modern = mock.post("/api/session").respond(404)
        legacy = mock.post("/rest/com/vmware/cis/session").respond(
            200, json={"value": "legacy-path-token"}
        )
        headers = await connector.auth_headers(_TARGET_A, _make_operator())

    assert modern.called and modern.call_count == 1
    assert legacy.called and legacy.call_count == 1
    assert headers == {"vmware-api-session-id": "legacy-path-token"}
    # The legacy path is recorded so aclose DELETEs against it.
    assert connector._session_paths == {target_cache_key(_TARGET_A): "/rest/com/vmware/cis/session"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_does_not_fall_back_on_401_from_modern_endpoint() -> None:
    """A 401 from /api/session is an auth failure, NOT an "endpoint missing" signal.

    Falling back to the legacy endpoint on 401 would mask credential
    problems and (worse) double the audit-log entries vCenter records
    for failed logins, tripping its built-in account-lockout protection.
    The fallback fires on 404 only.

    The legacy route is *deliberately not registered* — if the connector
    erroneously fell back to it, respx would raise an unhandled-request
    error, which is a stricter check than asserting ``not legacy.called``.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        modern = mock.post("/api/session").respond(401, json={"error": "invalid_credentials"})
        with pytest.raises(RuntimeError, match=r"vcenter-a") as exc_info:
            await connector.auth_headers(_TARGET_A, _make_operator())

    assert modern.called and modern.call_count == 1
    # The error message names the modern endpoint (the one that actually
    # responded 401) so operators don't chase a legacy-path red herring.
    assert "401" in str(exc_info.value)
    assert "/api/session" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_legacy_path_404_surfaces_runtime_error_naming_legacy_path() -> None:
    """When both paths 404, the error names the legacy endpoint (last attempted)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(404)
        with pytest.raises(RuntimeError, match=r"vcenter-a") as exc_info:
            await connector.auth_headers(_TARGET_A, _make_operator())

    # The last endpoint attempted is the one named — operators can see
    # in one log line that both paths failed.
    assert "/rest/com/vmware/cis/session" in str(exc_info.value)
    assert "404" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_aclose_revokes_against_legacy_path_when_fallback_was_used() -> None:
    """Mixed-path targets each get DELETE against the endpoint that minted the token.

    A target whose session lived on ``/api/session`` gets DELETE there;
    a target whose session lived on the legacy ``/rest/com/vmware/cis/session``
    gets DELETE there. Covers carry-over M2 from iter-1: the original
    DELETE-emission test only proved revocation against the modern path.
    With the fallback in place, mixed-path targets in a single connector
    instance must each revoke against the endpoint that minted their
    token.

    The modern DELETE on ``vcenter-b`` is *deliberately not registered* —
    if the connector erroneously DELETEd at ``/api/session`` for a
    target whose session was established at the legacy path, respx would
    raise an unhandled-request error.
    """
    connector = _make_connector()

    async with respx.mock() as mock:
        # _TARGET_A: modern path serves the session.
        mock.post("https://vcenter-a.test.invalid/api/session").respond(200, json="modern-token-a")
        delete_modern = mock.delete("https://vcenter-a.test.invalid/api/session").respond(204)
        # _TARGET_B: modern path 404s, legacy path serves the session.
        mock.post("https://vcenter-b.test.invalid/api/session").respond(404)
        mock.post("https://vcenter-b.test.invalid/rest/com/vmware/cis/session").respond(
            200, json="legacy-token-b"
        )
        delete_legacy = mock.delete(
            "https://vcenter-b.test.invalid/rest/com/vmware/cis/session"
        ).respond(204)

        await connector.auth_headers(_TARGET_A, _make_operator())
        await connector.auth_headers(_TARGET_B, _make_operator())
        await connector.aclose()

    # The load-bearing pair of assertions: each target was revoked at
    # the path that minted its token. ``vcenter-a`` -> modern DELETE,
    # ``vcenter-b`` -> legacy DELETE; the absence of a registered modern
    # DELETE route for vcenter-b means any drift would have surfaced
    # as an unhandled-request error before reaching these asserts.
    assert delete_modern.called and delete_modern.call_count == 1
    assert delete_legacy.called and delete_legacy.call_count == 1
    assert connector._session_paths == {}
    assert connector._session_tokens == {}


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_clear_error() -> None:
    """A loader that returns a dict without 'password' surfaces a clear message."""

    async def _bad_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        # Intentionally missing 'password'; a real production loader bug.
        return {"username": "svc-meho"}  # type: ignore[return-value]

    connector = VmwareRestConnector(session_loader=_bad_loader)
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"password"):
            await connector.auth_headers(_TARGET_A, _make_operator())
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
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="vcenter-per-user",
        host="vc.test.invalid",
        port=443,
        secret_ref="vsphere/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, _make_operator())

    assert "vcenter-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted as SHARED_SERVICE_ACCOUNT."""
    target = _StubTarget(
        name="vcenter-pre-g03",
        host="vc.test.invalid",
        port=443,
        secret_ref="vsphere/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vc.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="pre-g03-token")
        headers = await connector.auth_headers(target, _make_operator())

    assert headers == {"vmware-api-session-id": "pre-g03-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_value_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="vcenter-enum",
        host="vc.test.invalid",
        port=443,
        secret_ref="vsphere/enum",
    )
    # Use the enum member directly rather than its .value
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vc.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="enum-token")
        headers = await connector.auth_headers(target, _make_operator())

    assert headers == {"vmware-api-session-id": "enum-token"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose — session revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_revokes_every_cached_session() -> None:
    """aclose() issues DELETE /api/session for each cached token before pool tear-down."""
    connector = _make_connector()

    async with respx.mock() as mock:
        mock.post("https://vcenter-a.test.invalid/api/session").respond(200, json="token-for-a")
        mock.post("https://vcenter-b.test.invalid/api/session").respond(200, json="token-for-b")
        delete_a = mock.delete("https://vcenter-a.test.invalid/api/session").respond(204)
        delete_b = mock.delete("https://vcenter-b.test.invalid/api/session").respond(204)

        await connector.auth_headers(_TARGET_A, _make_operator())
        await connector.auth_headers(_TARGET_B, _make_operator())
        await connector.aclose()

    assert delete_a.called and delete_b.called
    # Both DELETE requests carried the session token in the header.
    deleted_headers_a = delete_a.calls[0].request.headers
    assert deleted_headers_a.get("vmware-api-session-id") == "token-for-a"
    deleted_headers_b = delete_b.calls[0].request.headers
    assert deleted_headers_b.get("vmware-api-session-id") == "token-for-b"
    # Pool cleared.
    assert connector._clients == {}
    assert connector._session_tokens == {}


@pytest.mark.asyncio
async def test_aclose_continues_on_revoke_failure() -> None:
    """A 5xx or transport error during DELETE /api/session is logged, not raised."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="token-for-a")
        # The revoke leg fails with a transport error — connector swallows
        # and proceeds (lifespan shutdown must not block).
        mock.delete("/api/session").mock(side_effect=httpx.ConnectError("revoke failed"))
        await connector.auth_headers(_TARGET_A, _make_operator())
        # The load-bearing invariant: aclose() returns cleanly.
        await connector.aclose()

    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_continues_on_revoke_4xx() -> None:
    """A 401/403 from DELETE /api/session is logged but doesn't block tear-down."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="token-for-a")
        # vCenter's DELETE /api/session may 401 if the session expired
        # between the last call and shutdown; tear-down still proceeds.
        mock.delete("/api/session").respond(401)
        await connector.auth_headers(_TARGET_A, _make_operator())
        await connector.aclose()

    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_sessions_is_a_noop() -> None:
    """A fresh connector with no sessions established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._session_tokens == {}


# ---------------------------------------------------------------------------
# TLS SNI / cert-verify override on session establish + revoke (#2398)
# ---------------------------------------------------------------------------


_TARGET_SNI = _StubTarget(
    name="vcenter-sni",
    host="vcenter-sni.test.invalid",
    port=443,
    secret_ref="vsphere/vcenter-sni",
    tls_server_name="vcenter.corp.example",
)


@pytest.mark.asyncio
async def test_establish_and_revoke_thread_sni_extension_modern_path() -> None:
    """#2398: the modern establish POST and the revoke DELETE carry the SNI override.

    A by-IP appliance whose cert pins an FQDN must offer ``tls_server_name``
    as the TLS SNI / cert-verify name on the login POST (before this fix the
    login bypassed the ``_request_extensions`` seam and failed
    ``CERTIFICATE_VERIFY_FAILED`` under ``verify_tls=true``) and on the
    best-effort shutdown revoke DELETE, which has no ``Target`` in scope.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vcenter-sni.test.invalid") as mock:
        post_route = mock.post("/api/session").respond(200, json="sni-token")
        delete_route = mock.delete("/api/session").respond(204)
        await connector.auth_headers(_TARGET_SNI, _make_operator())
        await connector.aclose()

    assert post_route.called
    assert post_route.calls[0].request.extensions["sni_hostname"] == "vcenter.corp.example"
    assert delete_route.called
    assert delete_route.calls[0].request.extensions["sni_hostname"] == "vcenter.corp.example"


@pytest.mark.asyncio
async def test_establish_threads_sni_extension_on_legacy_fallback() -> None:
    """#2398: the legacy-fallback establish POST also carries the SNI override."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-sni.test.invalid") as mock:
        mock.post("/api/session").respond(404)
        legacy_route = mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-sni")
        await connector.auth_headers(_TARGET_SNI, _make_operator())

    assert legacy_route.called
    assert legacy_route.calls[0].request.extensions["sni_hostname"] == "vcenter.corp.example"
    await connector.aclose()


@pytest.mark.asyncio
async def test_establish_omits_sni_extension_when_unset() -> None:
    """#2398 existing-behaviour: a target with no override dispatches with empty extensions."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-a.test.invalid") as mock:
        post_route = mock.post("/api/session").respond(200, json="plain-token")
        await connector.auth_headers(_TARGET_A, _make_operator())

    assert post_route.called
    assert "sni_hostname" not in post_route.calls[0].request.extensions
    await connector.aclose()
