# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`HarborConnector` auth + fingerprint/probe (G3.5-T7 #619).

Exercises HTTP Basic auth with admin and robot-account username forms,
per-target credential isolation, the auth_model boundary gate, and the
fingerprint/probe shapes against mocked Harbor 2.x endpoints.

Auth: no session token — HTTP Basic sent on every request. Credentials
cached per target so Vault is only queried once per target per connector
instance lifetime. No sso_realm suffix; username passed as-is from Vault
(supports both ``"admin"`` and robot forms like ``"robot$project+name"``).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    SYSTEM_OPERATOR_SUB,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.harbor import (
    HarborConnector,
    HarborTargetLike,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.settings import get_settings


def _make_operator(raw_jwt: str = "") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface."""
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
    """Pin the chassis env the shared credential loader now reads.

    Since #2642 the empty-``raw_jwt`` fail-closed guard lives on the *backend*
    (``VaultCredentialBackend.load_secret_data``) rather than ahead of the
    scheme split, so the loader resolves ``CREDENTIAL_BACKEND`` — and
    therefore ``Settings`` — before the guard fires. The behaviour under test
    is unchanged; it just needs the chassis env a running backplane always
    has.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> Iterator[None]:
    """Re-register HarborConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before every
    test in this module and clear after — same pattern
    :mod:`tests.test_connectors_sddc_manager_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies HarborTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    # Tenant-unique cache key components (#1642). Distinct ``id`` per
    # instance so two stub targets never collapse onto one cache entry.
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="harbor-a",
    host="harbor-a.test.invalid",
    port=443,
    secret_ref="harbor/harbor-a",
)
_TARGET_B = _StubTarget(
    name="harbor-b",
    host="harbor-b.test.invalid",
    port=443,
    secret_ref="harbor/harbor-b",
)


async def _stub_loader(_target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned admin credentials regardless of the target or operator."""
    return {"username": "admin", "password": "stub-password"}


async def _stub_robot_loader(_target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned robot-account credentials."""
    return {"username": "robot$myproject+myrobot", "password": "robot-secret"}


def _make_connector() -> HarborConnector:
    return HarborConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_harbor_connector_subclasses_http_connector() -> None:
    assert issubclass(HarborConnector, HttpConnector)
    assert HarborConnector.product == "harbor"
    assert HarborConnector.version == "2.x"
    assert HarborConnector.impl_id == "harbor-rest"
    assert HarborConnector.supported_version_range == ">=2.0,<3.0"
    assert HarborConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("harbor", "2.x", "harbor-rest")
    assert key in registry
    assert registry[key] is HarborConnector


def test_default_credentials_loader_delegates_to_shared_basic_loader() -> None:
    """The default loader is the thin wrapper around ``load_basic_credentials``.

    G3.10-T1 (#945) wired the live read; the loader now delegates to
    :func:`load_basic_credentials` rather than raising
    :exc:`NotImplementedError`. The fail-closed precondition (empty
    ``operator.raw_jwt``) is asserted via a :class:`VaultCredentialsReadError`
    on a system-initiated synthetic operator (no Vault is touched).
    """
    import asyncio

    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.harbor.session import load_credentials_from_vault

    async def _check() -> None:
        system_operator = _make_operator(raw_jwt="")
        with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
            await load_credentials_from_vault(_TARGET_A, system_operator)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# HTTP Basic auth — admin account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_basic_auth_for_admin_account() -> None:
    """auth_headers() produces Authorization: Basic with plain admin username."""
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == "admin"
    assert password == "stub-password"
    await connector.aclose()


# ---------------------------------------------------------------------------
# HTTP Basic auth — robot account username form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_basic_auth_for_robot_account_username_form() -> None:
    """robot$project+name username is passed as-is in the Basic auth header."""
    connector = HarborConnector(credentials_loader=_stub_robot_loader)
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == "robot$myproject+myrobot"
    assert password == "robot-secret"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_credentials_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin", "password": "stub-password"}

    connector = HarborConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2
    assert call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# System-operator cache-bypass (fail-closed; #1008)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_cache_not_served_to_system_operator_runs_loader() -> None:
    """A warm cache primed by a real operator is NOT served to the system operator.

    The system/operator-less caller (``synthesise_system_operator``) must
    re-run the loader so its fail-closed behaviour applies — it can never
    borrow warm credentials a real operator resolved (#1008). Keyed off
    ``SYSTEM_OPERATOR_SUB``, not ``raw_jwt`` (the system operator carries a
    non-empty placeholder JWT since #980).
    """
    call_log: list[str] = []

    async def _counting_loader(_target: HarborTargetLike, operator: Operator) -> dict[str, str]:
        call_log.append(operator.sub)
        return {"username": "admin", "password": "stub-password"}

    connector = HarborConnector(credentials_loader=_counting_loader)
    # Real operator warms the cache (cold load).
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    # System operator must re-run the loader rather than reuse the warm cache.
    await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())

    assert call_log == ["test-operator", SYSTEM_OPERATOR_SUB]
    await connector.aclose()


@pytest.mark.asyncio
async def test_system_operator_fails_closed_against_warm_cache() -> None:
    """With a warm cache, a system-operator load runs the loader and fails closed.

    Proves the bypass is closed end to end: even though a real operator
    primed the cache, the system caller hits the loader, which fails closed
    per its contract (a system-initiated read cannot resolve per-target
    credentials).
    """

    async def _failing_for_system_loader(
        _target: HarborTargetLike, operator: Operator
    ) -> dict[str, str]:
        if operator.sub == SYSTEM_OPERATOR_SUB:
            raise VaultCredentialsReadError(
                "system-initiated calls cannot read per-target vendor credentials"
            )
        return {"username": "admin", "password": "stub-password"}

    connector = HarborConnector(credentials_loader=_failing_for_system_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm the cache
    with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
        await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_real_operator_reuse_unchanged_after_system_operator_call() -> None:
    """Real-operator reuse is unaffected by the system-operator cache bypass.

    A second real-operator call still reuses the warm cache (loader called
    once for the real operator); only the interleaved system-operator call
    re-runs the loader.
    """
    real_calls = 0

    async def _counting_loader(_target: HarborTargetLike, operator: Operator) -> dict[str, str]:
        nonlocal real_calls
        if operator.sub != SYSTEM_OPERATOR_SUB:
            real_calls += 1
        return {"username": "admin", "password": "stub-password"}

    connector = HarborConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # cold real load
    await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())  # bypass
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm real reuse

    assert real_calls == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-target isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_credentials_separate() -> None:
    """Two targets get two distinct credential cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = HarborConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    username_a, _ = _decode_basic_auth(h_a["Authorization"])
    username_b, _ = _decode_basic_auth(h_b["Authorization"])
    assert username_a == "svc-harbor-a"
    assert username_b == "svc-harbor-b"
    assert call_log == ["harbor-a", "harbor-b"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_credentials() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached credential.

    Regression guard for #1642: the credential cache used to key on
    ``target.name`` alone, so two same-named targets in different tenants
    collapsed onto one entry and one tenant could be served another
    tenant's cached credential. The cache keys on the tenant-unique
    ``(tenant_id, id)`` tuple instead.
    """
    load_count = 0

    async def _counting_loader(target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal load_count
        load_count += 1
        return {"username": f"svc-{target.tenant_id}", "password": "pass"}

    tenant_one = _StubTarget(
        name="harbor-shared",
        host="harbor-shared.test.invalid",
        port=443,
        secret_ref="harbor/harbor-shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="harbor-shared",
        host="harbor-shared.test.invalid",
        port=443,
        secret_ref="harbor/harbor-shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    connector = HarborConnector(credentials_loader=_counting_loader)
    h_one = await connector.auth_headers(tenant_one, operator=_make_operator())
    h_two = await connector.auth_headers(tenant_two, operator=_make_operator())

    user_one, _ = _decode_basic_auth(h_one["Authorization"])
    user_two, _ = _decode_basic_auth(h_two["Authorization"])
    # Each tenant triggered its own load -- no cross-tenant cache hit.
    assert load_count == 2
    assert user_one == f"svc-{tenant_one.tenant_id}"
    assert user_two == f"svc-{tenant_two.tenant_id}"
    assert user_one != user_two
    assert connector._creds_cache.keys() == {
        target_cache_key(tenant_one),
        target_cache_key(tenant_two),
    }

    # Same-tenant re-fetch is a cache HIT -- behaviour unchanged.
    h_one_again = await connector.auth_headers(tenant_one, operator=_make_operator())
    assert h_one_again == h_one
    assert load_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "admin"}  # type: ignore[return-value]

    connector = HarborConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "harbor-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: HarborTargetLike, _operator: Operator) -> dict[str, str]:
        return {"password": "stub-password"}  # type: ignore[return-value]

    connector = HarborConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "harbor-a" in str(exc_info.value)
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
        name="harbor-per-user",
        host="harbor.test.invalid",
        port=443,
        secret_ref="harbor/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "harbor-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="harbor-pre-g03",
        host="harbor.test.invalid",
        port=443,
        secret_ref="harbor/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="harbor-enum",
        host="harbor.test.invalid",
        port=443,
        secret_ref="harbor/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against mocked GET /api/v2.0/systeminfo returns canonical shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://harbor-a.test.invalid") as mock:
        mock.get("/api/v2.0/systeminfo").respond(
            200,
            json={
                "harbor_version": "v2.11.0-abc1234def",
                "auth_mode": "db_auth",
                "registry_url": "harbor-a.test.invalid",
                "external_url": "https://harbor-a.test.invalid",
                "self_registration": False,
                "has_ca_root": False,
                "with_notary": False,
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "harbor"
    assert fp.version == "v2.11.0"
    assert fp.build == "abc1234def"
    assert fp.reachable is True
    assert fp.probe_method == "GET /api/v2.0/systeminfo"
    assert fp.extras["auth_mode"] == "db_auth"
    assert fp.extras["registry_url"] == "harbor-a.test.invalid"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_version_without_build_hash() -> None:
    """A bare harbor_version string (no - separator) leaves build=None."""
    connector = _make_connector()

    async with respx.mock(base_url="https://harbor-a.test.invalid") as mock:
        mock.get("/api/v2.0/systeminfo").respond(
            200,
            json={"harbor_version": "v2.11.0", "auth_mode": "ldap_auth"},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.version == "v2.11.0"
    assert fp.build is None
    assert fp.reachable is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_structured_error() -> None:
    """Transport/status failure returns reachable=False with extras['error']."""
    connector = _make_connector()

    async with respx.mock(base_url="https://harbor-a.test.invalid") as mock:
        mock.get("/api/v2.0/systeminfo").respond(401, json={"errors": [{"code": "UNAUTHORIZED"}]})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "harbor"
    assert fp.reachable is False
    assert fp.probe_method == "GET /api/v2.0/systeminfo"
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "401" in error
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_all_components_healthy() -> None:
    """probe() returns ok=True when Harbor's health endpoint reports all healthy."""
    connector = _make_connector()

    async with respx.mock(base_url="https://harbor-a.test.invalid") as mock:
        mock.get("/api/v2.0/health").respond(
            200,
            json={
                "status": "healthy",
                "components": [
                    {"name": "database", "status": "healthy"},
                    {"name": "jobservice", "status": "healthy"},
                    {"name": "redis", "status": "healthy"},
                    {"name": "registry", "status": "healthy"},
                ],
            },
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_for_unhealthy_component() -> None:
    """probe() returns ok=False with reason listing unhealthy component names."""
    connector = _make_connector()

    async with respx.mock(base_url="https://harbor-a.test.invalid") as mock:
        mock.get("/api/v2.0/health").respond(
            200,
            json={
                "status": "unhealthy",
                "components": [
                    {"name": "database", "status": "healthy"},
                    {"name": "jobservice", "status": "unhealthy", "error": "connection refused"},
                    {"name": "redis", "status": "unhealthy", "error": "timeout"},
                    {"name": "registry", "status": "healthy"},
                ],
            },
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "jobservice" in result.reason
    assert "redis" in result.reason
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_on_transport_error() -> None:
    """probe() returns ok=False + reason on transport failure."""
    connector = _make_connector()

    async with respx.mock(base_url="https://harbor-a.test.invalid") as mock:
        mock.get("/api/v2.0/health").respond(503)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_credential_cache_and_pool() -> None:
    """aclose() clears the in-memory credential cache and tears down the httpx pool."""
    connector = _make_connector()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert target_cache_key(_TARGET_A) in connector._creds_cache
    await connector.aclose()
    assert connector._creds_cache == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_credentials_is_a_noop() -> None:
    """A fresh connector with no credentials established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._creds_cache == {}
