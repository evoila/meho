# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`ArgoCdConnector` auth + fingerprint/probe (G3.12-T1 #1390).

Exercises bearer-token auth, per-target credential isolation + caching, the
system-operator cache-bypass (#1008), the auth_model boundary gate, and the
fingerprint/probe shapes against mocked ArgoCD ``argocd-server`` endpoints.

Auth: no session token — ``Authorization: Bearer <token>`` sent on every
request. The token is cached per target so Vault is only queried once per
target per connector instance lifetime. Unlike Harbor's Basic auth there is
no username component; the stored token is sent verbatim.
"""

from __future__ import annotations

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
from meho_backplane.connectors.argocd import (
    ArgoCdConnector,
    ArgoCdTargetLike,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel


def _make_operator(raw_jwt: str = "op.jwt.value") -> Operator:
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
def _clean_argocd_registry() -> Iterator[None]:
    """Re-register ArgoCdConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before every
    test in this module and clear after — same pattern
    :mod:`tests.test_connectors_harbor_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=ArgoCdConnector.product,
        version=ArgoCdConnector.version,
        impl_id=ArgoCdConnector.impl_id,
        cls=ArgoCdConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies ArgoCdTargetLike Protocol structurally.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    # Tenant-unique cache key components (#1642/#1672).
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="argocd-infra",
    host="argocd-infra.test.invalid",
    port=443,
    secret_ref="targets/argocd-infra",
)
_TARGET_B = _StubTarget(
    name="argocd-ci",
    host="argocd-ci.test.invalid",
    port=443,
    secret_ref="targets/argocd-ci",
)


async def _stub_loader(_target: ArgoCdTargetLike, _operator: Operator) -> dict[str, str]:
    """Return a canned bearer token regardless of the target or operator."""
    return {"token": "stub-bearer-token"}


def _make_connector() -> ArgoCdConnector:
    return ArgoCdConnector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_argocd_connector_subclasses_http_connector() -> None:
    assert issubclass(ArgoCdConnector, HttpConnector)
    assert ArgoCdConnector.product == "argocd"
    assert ArgoCdConnector.version == "3.x"
    assert ArgoCdConnector.impl_id == "argocd-api"
    assert ArgoCdConnector.supported_version_range == ">=2.0,<4.0"
    assert ArgoCdConnector.priority == 1


def test_importing_package_registers_versioned_and_wildcard_v2_entries() -> None:
    """The package registers both the versioned triple and the wildcard fallback."""
    import importlib

    import meho_backplane.connectors.argocd as argocd_pkg

    # The autouse fixture pre-registered only the versioned triple; clear it
    # so the reloaded module body can re-run both register_connector_v2 calls
    # (versioned + wildcard) without colliding with the duplicate guard.
    clear_registry()
    importlib.reload(argocd_pkg)

    registry = all_connectors_v2()
    versioned = ("argocd", "3.x", "argocd-api")
    wildcard = ("argocd", "", "")
    assert registry[versioned] is ArgoCdConnector
    assert registry[wildcard] is ArgoCdConnector


def test_default_credentials_loader_delegates_to_shared_basic_loader() -> None:
    """The default loader is the thin wrapper around ``load_basic_credentials``.

    The fail-closed precondition (empty ``operator.raw_jwt``) is asserted
    via a :class:`VaultCredentialsReadError` on a system-initiated operator
    (no Vault is touched).
    """
    import asyncio

    from meho_backplane.connectors.argocd.session import load_credentials_from_vault

    async def _check() -> None:
        system_operator = _make_operator(raw_jwt="")
        with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
            await load_credentials_from_vault(_TARGET_A, system_operator)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Bearer-token auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_bearer_token() -> None:
    """auth_headers() produces ``Authorization: Bearer <token>``."""
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert headers == {"Authorization": "Bearer stub-bearer-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_token_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: ArgoCdTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"token": "stub-bearer-token"}

    connector = ArgoCdConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2
    assert call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_tokens_separate() -> None:
    """Two targets get two distinct cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: ArgoCdTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"token": f"token-{target.name}"}

    connector = ArgoCdConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    assert h_a == {"Authorization": "Bearer token-argocd-infra"}
    assert h_b == {"Authorization": "Bearer token-argocd-ci"}
    assert call_log == ["argocd-infra", "argocd-ci"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_tokens() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached token.

    Regression guard for #1642/#1672: the credential cache used to key on
    ``target.name`` alone, so two same-named targets in different tenants
    collapsed onto one entry and one tenant could be served another
    tenant's token. The cache keys on the tenant-unique ``(tenant_id, id)``
    tuple instead.
    """
    tenant_one = _StubTarget(
        name="argocd-shared",
        host="argocd-shared.test.invalid",
        port=443,
        secret_ref="targets/argocd-shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="argocd-shared",
        host="argocd-shared.test.invalid",
        port=443,
        secret_ref="targets/argocd-shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )
    call_log: list[tuple[str, str]] = []

    async def _tracking_loader(target: ArgoCdTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append((str(target.tenant_id), str(target.id)))
        return {"token": f"token-{target.tenant_id}"}

    connector = ArgoCdConnector(credentials_loader=_tracking_loader)
    h_one = await connector.auth_headers(tenant_one, operator=_make_operator())
    h_two = await connector.auth_headers(tenant_two, operator=_make_operator())

    # Each tenant loaded its own token — no cross-tenant cache hit.
    assert h_one != h_two
    assert len(call_log) == 2
    assert connector._creds_cache == {
        target_cache_key(tenant_one): {"token": f"token-{tenant_one.tenant_id}"},
        target_cache_key(tenant_two): {"token": f"token-{tenant_two.tenant_id}"},
    }

    # Same-tenant re-fetch is a cache HIT — loader not re-invoked.
    await connector.auth_headers(tenant_one, operator=_make_operator())
    assert len(call_log) == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# System-operator cache-bypass (fail-closed; #1008)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_cache_not_served_to_system_operator_runs_loader() -> None:
    """A warm cache primed by a real operator is NOT served to the system operator."""
    call_log: list[str] = []

    async def _counting_loader(_target: ArgoCdTargetLike, operator: Operator) -> dict[str, str]:
        call_log.append(operator.sub)
        return {"token": "stub-bearer-token"}

    connector = ArgoCdConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())

    assert call_log == ["test-operator", SYSTEM_OPERATOR_SUB]
    await connector.aclose()


@pytest.mark.asyncio
async def test_real_operator_reuse_unchanged_after_system_operator_call() -> None:
    """Real-operator reuse is unaffected by the system-operator cache bypass."""
    real_calls = 0

    async def _counting_loader(_target: ArgoCdTargetLike, operator: Operator) -> dict[str, str]:
        nonlocal real_calls
        if operator.sub != SYSTEM_OPERATOR_SUB:
            real_calls += 1
        return {"token": "stub-bearer-token"}

    connector = ArgoCdConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # cold real load
    await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())  # bypass
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm real reuse

    assert real_calls == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_token_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: ArgoCdTargetLike, _operator: Operator) -> dict[str, str]:
        return {}

    connector = ArgoCdConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"token") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "argocd-infra" in str(exc_info.value)
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
        name="argocd-per-user",
        host="argocd.test.invalid",
        port=443,
        secret_ref="targets/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "argocd-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="argocd-pre-g03",
        host="argocd.test.invalid",
        port=443,
        secret_ref="targets/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Bearer ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="argocd-enum",
        host="argocd.test.invalid",
        port=443,
        secret_ref="targets/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Bearer ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against mocked GET /api/version returns the canonical shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://argocd-infra.test.invalid") as mock:
        mock.get("/api/version").respond(
            200,
            json={
                "Version": "v3.3.9+abc1234",
                "BuildDate": "2026-01-15T00:00:00Z",
                "GitCommit": "abc1234",
                "KustomizeVersion": "v5.4.3 2024-05-16",
                "HelmVersion": "v3.15.2+g1a500d5",
                "KubectlVersion": "v0.30.1",
                "JsonnetVersion": "v0.20.0",
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "argoproj"
    assert fp.product == "argocd"
    assert fp.version == "v3.3.9+abc1234"
    assert fp.reachable is True
    assert fp.probe_method == "GET /api/version"
    assert fp.extras["KustomizeVersion"] == "v5.4.3 2024-05-16"
    assert fp.extras["HelmVersion"] == "v3.15.2+g1a500d5"
    assert fp.extras["KubectlVersion"] == "v0.30.1"
    assert fp.extras["BuildDate"] == "2026-01-15T00:00:00Z"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_does_not_require_a_bearer_token() -> None:
    """``GET /api/version`` is unauthenticated — fingerprint never calls the loader."""

    async def _exploding_loader(_target: ArgoCdTargetLike, _operator: Operator) -> dict[str, str]:
        raise AssertionError("fingerprint must not read the bearer token")

    connector = ArgoCdConnector(credentials_loader=_exploding_loader)

    async with respx.mock(base_url="https://argocd-infra.test.invalid") as mock:
        mock.get("/api/version").respond(200, json={"Version": "v3.3.9"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is True
    assert fp.version == "v3.3.9"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_structured_error() -> None:
    """Transport/status failure returns reachable=False with extras['error']."""
    connector = _make_connector()

    async with respx.mock(base_url="https://argocd-infra.test.invalid") as mock:
        mock.get("/api/version").respond(503, json={"error": "unavailable"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "argoproj"
    assert fp.product == "argocd"
    assert fp.reachable is False
    assert fp.probe_method == "GET /api/version"
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "503" in error
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_version_reachable() -> None:
    """probe() returns ok=True when GET /api/version round-trips."""
    connector = _make_connector()

    async with respx.mock(base_url="https://argocd-infra.test.invalid") as mock:
        mock.get("/api/version").respond(200, json={"Version": "v3.3.9"})
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_on_transport_error() -> None:
    """probe() returns ok=False + reason on transport/status failure."""
    connector = _make_connector()

    async with respx.mock(base_url="https://argocd-infra.test.invalid") as mock:
        mock.get("/api/version").respond(503)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_token_cache_and_pool() -> None:
    """aclose() clears the in-memory token cache and tears down the httpx pool."""
    connector = _make_connector()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert target_cache_key(_TARGET_A) in connector._creds_cache
    await connector.aclose()
    assert connector._creds_cache == {}
    assert connector._clients == {}
