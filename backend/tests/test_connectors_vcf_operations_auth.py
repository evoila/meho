# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VcfOperationsConnector` (G3.6-T1 #829, rebuilt #2395).

Exercises the acquired-token (``OpsToken``) session auth VCF Operations
9.0.2 requires — stateless HTTP Basic is rejected by the live appliance:

* ``POST /suite-api/api/auth/token/acquire`` with a JSON body
  ``{username, password}`` (plus ``authSource`` when the target federates
  identity).
* Response body ``{"token": "<t>", "validity": ..., "expiresAt": ...,
  "roles": []}`` — ``token`` is extracted and cached per target.
* Downstream auth header: ``Authorization: OpsToken <token>`` (never Basic,
  never Bearer).
* ``invalidate_session`` — the duck-typed dispatch-path eviction hook (#2067).
* Per-target token isolation, the ``auth_model`` boundary gate, and the
  fingerprint/probe shapes against a mocked vROps ``/suite-api/api/*``.

The contract mirrors :mod:`tests.test_connectors_vcf_logs_auth` with vROps
divergence: the acquire path/body/response shape, the ``authSource`` field on
the acquire body (not a per-request query param), and the ``OpsToken`` scheme.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_operations import (
    VcfOperationsConnector,
    VcfOperationsTargetLike,
)

# ---------------------------------------------------------------------------
# OpsToken session-establish path + canned acquire response (#2395).
# ---------------------------------------------------------------------------

_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_VERSIONS_PATH = "/suite-api/api/versions/current"
_OPS_TOKEN = "vrops-ops-token-abc-123"


def _acquire_response(token: str = _OPS_TOKEN) -> dict[str, Any]:
    """A canonical ``token/acquire`` 200 body carrying *token*."""
    return {
        "token": token,
        "validity": 1470421325035,
        "expiresAt": "Friday, August 5, 2016 6:22:05 PM UTC",
        "roles": [],
    }


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
def _clean_vcf_operations_registry() -> Iterator[None]:
    """Re-register VcfOperationsConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that calls
    :func:`clear_registry` between tests. Re-register before every test in
    this module and clear after — same pattern
    :mod:`tests.test_connectors_harbor_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=VcfOperationsConnector.product,
        version=VcfOperationsConnector.version,
        impl_id=VcfOperationsConnector.impl_id,
        cls=VcfOperationsConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies VcfOperationsTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands ``auth_source``.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    auth_source: str | None = None
    # Tenant-unique cache key components (#1642). Distinct ``id`` per
    # instance so two stub targets never collapse onto one cache entry.
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="vrops-a",
    host="vrops-a.test.invalid",
    port=443,
    secret_ref="vrops/vrops-a",
)
_TARGET_B = _StubTarget(
    name="vrops-b",
    host="vrops-b.test.invalid",
    port=443,
    secret_ref="vrops/vrops-b",
)
_TARGET_WITH_AUTH_SOURCE = _StubTarget(
    name="vrops-ad",
    host="vrops-ad.test.invalid",
    port=443,
    secret_ref="vrops/vrops-ad",
    auth_source="corp-ad",
)


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned local-account credentials regardless of the target."""
    return {"username": "admin", "password": "stub-password"}


def _make_connector() -> VcfOperationsConnector:
    return VcfOperationsConnector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vcf_operations_connector_subclasses_http_connector() -> None:
    assert issubclass(VcfOperationsConnector, HttpConnector)
    assert VcfOperationsConnector.product == "vrops"
    assert VcfOperationsConnector.version == "9.0"
    assert VcfOperationsConnector.impl_id == "vrops-rest"
    assert VcfOperationsConnector.supported_version_range == ">=9.0,<10.0"
    assert VcfOperationsConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vrops", "9.0", "vrops-rest")
    assert key in registry
    assert registry[key] is VcfOperationsConnector


def test_default_credentials_loader_fails_closed_without_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default loader is the live shared operator-context Vault read (G3.10-T2).

    Empty ``raw_jwt`` is fail-closed — system-initiated calls have no
    operator JWT to forward to Vault's JWT/OIDC auth method, so the
    helper raises :class:`VaultCredentialsReadError` rather than
    silently falling back to a backplane identity. End-to-end coverage
    of the wired read lives in ``test_connectors_vcf_operations_credread.py``.
    """
    import asyncio

    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.vcf_operations.session import (
        load_credentials_from_vault,
    )
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match=r"vrops-a"):
            await load_credentials_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    try:
        asyncio.run(_check())
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Session establishment — happy path (OpsToken)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_acquires_token_and_returns_opstoken() -> None:
    """First auth_headers call POSTs JSON creds to token/acquire and returns OpsToken.

    Asserts the load-bearing auth contract: a JSON body (NOT form, NOT HTTP
    Basic) with ``username`` + ``password`` (no ``authSource`` for a
    local-realm target), the acquire POST carrying no stale Authorization
    header, and the returned token presented as ``Authorization: OpsToken``.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        acquire_route = mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert acquire_route.called and acquire_route.call_count == 1
    assert headers == {"Authorization": f"OpsToken {_OPS_TOKEN}"}

    request = acquire_route.calls[0].request
    assert request.headers.get("content-type", "").startswith("application/json")
    body = json.loads(request.read().decode())
    assert body == {"username": "admin", "password": "stub-password"}
    # The acquire POST itself must NOT carry a stale Authorization header.
    assert "authorization" not in {k.lower() for k in request.headers}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_carries_authsource_in_acquire_body_when_set() -> None:
    """target.auth_source lands as ``authSource`` in the token/acquire body."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-ad.test.invalid") as mock:
        acquire_route = mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        headers = await connector.auth_headers(_TARGET_WITH_AUTH_SOURCE, operator=_make_operator())

    assert headers == {"Authorization": f"OpsToken {_OPS_TOKEN}"}
    body = json.loads(acquire_route.calls[0].request.read().decode())
    assert body == {
        "username": "admin",
        "password": "stub-password",
        "authSource": "corp-ad",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_session_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-POST token/acquire."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        acquire_route = mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2 == {"Authorization": f"OpsToken {_OPS_TOKEN}"}
    # The load-bearing assertion — exactly one token/acquire for two calls.
    assert acquire_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_session_tokens_separate() -> None:
    """Two targets get two distinct cached tokens; no cross-target leakage."""
    connector = _make_connector()

    async with respx.mock() as mock:
        route_a = mock.post("https://vrops-a.test.invalid" + _ACQUIRE_PATH).respond(
            200, json=_acquire_response("token-a")
        )
        route_b = mock.post("https://vrops-b.test.invalid" + _ACQUIRE_PATH).respond(
            200, json=_acquire_response("token-b")
        )

        h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    assert route_a.called and route_b.called
    assert h_a == {"Authorization": "OpsToken token-a"}
    assert h_b == {"Authorization": "OpsToken token-b"}
    assert connector._session_tokens == {
        target_cache_key(_TARGET_A): "token-a",
        target_cache_key(_TARGET_B): "token-b",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_public_invalidate_session_evicts_only_the_targets_slot() -> None:
    """Public ``invalidate_session`` (the #2067 dispatch-path hook) evicts one slot.

    Delegates to ``_invalidate_session``; seeds two targets and asserts only
    A's token is dropped, so the dispatcher's re-acquire of a dispatched vROps
    op preserves per-``(tenant_id, target.id)`` isolation.
    """
    connector = _make_connector()
    key_a = target_cache_key(_TARGET_A)
    key_b = target_cache_key(_TARGET_B)
    connector._session_tokens[key_a] = "token-a"
    connector._session_tokens[key_b] = "token-b"

    await connector.invalidate_session(_TARGET_A)

    assert connector._session_tokens == {key_b: "token-b"}
    # No-op on an already-evicted target.
    await connector.invalidate_session(_TARGET_A)
    assert connector._session_tokens == {key_b: "token-b"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# Session establishment — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_401_surfaces_session_login_error_naming_target() -> None:
    """401 from token/acquire raises SessionLoginError (ConnectorAuthError) naming the target."""
    from meho_backplane.connectors.vcf_operations import SessionLoginError

    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(401, json={"message": "invalid_credentials"})
        with pytest.raises(SessionLoginError, match=r"vrops-a") as exc_info:
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "401" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_invalidate_credentials_reloads_after_restage() -> None:
    """#2396: invalidate_credentials drops the credential cache so a restage is re-read.

    Establish-failure flow (post-#2395 OpsToken session): the first acquire is
    rejected because the staged credential is wrong, so no session token is
    cached and the connector still holds the wrong credential bytes in the
    shared :class:`CredentialsCache` (read at line 314 *before* the acquire).
    Without eviction the next ``auth_headers`` re-reads that cached wrong
    credential (loader call-count stays 1) and re-acquires with it forever —
    the bug #2396 fixes. The dispatcher's establish-auth arm calls the new
    duck-typed ``invalidate_credentials`` hook (which delegates to
    :meth:`CredentialsCache.invalidate`); after the operator restages, the next
    ``auth_headers`` re-runs the loader (call-count 2), reads the corrected
    credential, and the acquire succeeds — with **no backplane restart**. Red
    before #2396: ``invalidate_credentials`` did not exist.
    """
    from meho_backplane.connectors.vcf_operations import SessionLoginError

    call_count = 0
    creds = {"username": "svc-old", "password": "old-pass"}

    async def _swappable_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return dict(creds)

    connector = VcfOperationsConnector(credentials_loader=_swappable_loader)

    # The staged credential is wrong: the first token/acquire is rejected.
    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(401, json={"message": "invalid_credentials"})
        with pytest.raises(SessionLoginError):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())
    # Loader ran once and the wrong credential is now cached (read before acquire).
    assert call_count == 1

    # The dispatcher evicts the cached (wrong) credential on the establish-auth failure.
    await connector.invalidate_credentials(_TARGET_A)

    # The operator restages the corrected credential out of band.
    creds.update(username="svc-new", password="new-pass")

    # Next dispatch re-reads Vault (loader call-count 2) and the acquire succeeds.
    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        acquire_route = mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert call_count == 2
    assert acquire_route.called
    assert headers == {"Authorization": f"OpsToken {_OPS_TOKEN}"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_acquire_missing_token_in_body_raises() -> None:
    """A 2xx response without a ``token`` field raises naming the target.

    A misbehaving proxy can 200 with an empty body or the wrong field name;
    the connector fails loudly rather than caching an empty token that would
    silently 401 every subsequent call.
    """
    from meho_backplane.connectors.vcf_operations import SessionLoginError

    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"validity": 1470421325035})
        with pytest.raises(SessionLoginError, match=r"vrops-a"):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    await connector.aclose()


@pytest.mark.asyncio
async def test_acquire_empty_token_raises() -> None:
    """A 2xx response with an empty-string token raises naming the target."""
    from meho_backplane.connectors.vcf_operations import SessionLoginError

    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": "", "validity": 1})
        with pytest.raises(SessionLoginError, match=r"vrops-a"):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes (raised before any acquire round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "admin"}

    connector = VcfOperationsConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "vrops-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"password": "stub-password"}

    connector = VcfOperationsConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "vrops-a" in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating (rejected before any acquire round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(
    auth_model: str,
) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="vrops-per-user",
        host="vrops.test.invalid",
        port=443,
        secret_ref="vrops/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "vrops-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="vrops-pre-g03",
        host="vrops-pre-g03.test.invalid",
        port=443,
        secret_ref="vrops/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops-pre-g03.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response("pre-g03-token"))
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "OpsToken pre-g03-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="vrops-enum",
        host="vrops-enum.test.invalid",
        port=443,
        secret_ref="vrops/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops-enum.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response("enum-token"))
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "OpsToken enum-token"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against mocked GET /suite-api/api/versions/current returns canonical shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        mock.get(_VERSIONS_PATH).respond(
            200,
            json={
                "releaseName": "9.0.0",
                "buildNumber": 23456789,
                "humanlyReadableReleaseName": "VMware Aria Operations 9.0",
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vrops"
    assert fp.version == "9.0.0"
    assert fp.build == "23456789"
    assert fp.reachable is True
    assert fp.probe_method == "GET /suite-api/api/versions/current"
    assert fp.extras["humanly_readable_release_name"] == "VMware Aria Operations 9.0"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_humanly_readable_name() -> None:
    """A vROps response missing humanlyReadableReleaseName leaves the extras key None."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        mock.get(_VERSIONS_PATH).respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 23456789},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.version == "9.0.0"
    assert fp.build == "23456789"
    assert fp.reachable is True
    assert fp.extras["humanly_readable_release_name"] is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_structured_error() -> None:
    """A 401 on the read (after a successful acquire) returns reachable=False with error."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        mock.get(_VERSIONS_PATH).respond(401, json={"error": "unauthorised"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vrops"
    assert fp.reachable is False
    assert fp.probe_method == "GET /suite-api/api/versions/current"
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "401" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_acquire_failure() -> None:
    """A 401 at token/acquire also yields reachable=False (session establish failed)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(401, json={"message": "invalid_credentials"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is False
    assert "error" in fp.extras
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_true_on_reachable_target() -> None:
    """probe() returns ok=True when the version endpoint is reachable (delegates to fingerprint)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        mock.get(_VERSIONS_PATH).respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 23456789},
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_on_transport_error() -> None:
    """probe() returns ok=False + reason on transport/status failure."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        # 401 is non-retryable (4xx) — surfaces immediately on the
        # fingerprint call which probe() delegates to.
        mock.get(_VERSIONS_PATH).respond(401)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "HTTPStatusError" in result.reason or "401" in result.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_session_and_credential_caches_and_pool() -> None:
    """aclose() clears the in-memory session + credential caches and tears down the pool."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert connector._session_tokens == {target_cache_key(_TARGET_A): _OPS_TOKEN}
    assert target_cache_key(_TARGET_A) in connector._creds.cached_targets
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._creds.cached_targets == frozenset()
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_session_is_a_noop() -> None:
    """A fresh connector with no session established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._session_tokens == {}
    assert connector._creds.cached_targets == frozenset()


# ---------------------------------------------------------------------------
# Downstream 401 re-acquire — belt-and-suspenders on the httpx side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_then_auth_headers_reacquires_a_fresh_token() -> None:
    """After ``invalidate_session``, the next auth_headers re-acquires a fresh token.

    The unit-level shape of the #2067 dispatch-path recovery: evicting the
    cached token forces a fresh ``token/acquire`` on the next call. The
    end-to-end 401 → re-dispatch recovery is pinned in
    ``test_connectors_vcf_operations_credread.py``.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        acquire_route = mock.post(_ACQUIRE_PATH)
        acquire_route.side_effect = [
            httpx.Response(200, json=_acquire_response("token-first")),
            httpx.Response(200, json=_acquire_response("token-second")),
        ]
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        await connector.invalidate_session(_TARGET_A)
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == {"Authorization": "OpsToken token-first"}
    assert h2 == {"Authorization": "OpsToken token-second"}
    assert acquire_route.call_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_stub_target_satisfies_protocol_at_runtime() -> None:
    """_StubTarget satisfies the runtime-checkable VcfOperationsTargetLike Protocol.

    Guards against drift between the Protocol shape and the test fixture: a
    new required field on the Protocol that the stub doesn't carry would fail
    this check before the rest of the suite produces confusing AttributeErrors.
    """
    assert isinstance(_TARGET_A, VcfOperationsTargetLike)
    assert isinstance(_TARGET_WITH_AUTH_SOURCE, VcfOperationsTargetLike)
