# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-scope guard for the agent-supplied ``vault.kv.*`` ops (#1643).

Defense-in-depth: a caller requesting a path outside their tenant
namespace is denied **before** the hvac call. These tests cover the
:func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`
helper directly (boundary cases) and every KV-v2 handler end-to-end
through the real dispatcher (the AC: all handlers deny out-of-namespace
requests; in-namespace requests are unchanged).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.connector import _SYSTEM_TENANT_ID
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.connectors.vault.tenant_scope import (
    PLATFORM_EXEMPT_PATHS,
    VaultTenantScopeError,
    enforce_tenant_scope,
    rendered_tenant_prefix,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# A concrete, non-system tenant so the guard does not short-circuit on the
# Nil-UUID system-operator exemption.
_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

# The opt-in convention these tests enforce: each tenant's KV calls must
# live under ``tenant-<tenant_id>/`` on the (default) ``secret`` mount.
_SCOPE_TEMPLATE = "secret/tenant-{tenant_id}/"


# ---------------------------------------------------------------------------
# Fixtures — register the vault connector + typed ops, pin env
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registered_connector() -> Iterator[None]:
    clear_registry()
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=VaultConnector,
    )
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    # Guard enabled for this suite — the per-tenant prefix convention.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", _SCOPE_TEMPLATE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_vault_typed_ops(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_vault_typed_operations(embedding_service=stub_embedding_service)


def _operator(tenant_id: UUID, *, jwt: str = "fake.jwt.value") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch(
    op_id: str,
    params: dict[str, Any],
    *,
    tenant_id: UUID,
) -> OperationResult:
    return await dispatch(
        operator=_operator(tenant_id),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=True,
    )


# ---------------------------------------------------------------------------
# Unit — enforce_tenant_scope boundary behaviour
# ---------------------------------------------------------------------------


def test_rendered_prefix_uses_canonical_uuid() -> None:
    op = _operator(_TENANT_A)
    assert rendered_tenant_prefix(op, template=_SCOPE_TEMPLATE) == f"secret/tenant-{_TENANT_A}/"


def test_in_namespace_path_passes() -> None:
    """A path under the operator's rendered prefix is allowed (no raise)."""
    op = _operator(_TENANT_A)
    # mount "secret", path "tenant-<A>/db/creds" → "secret/tenant-<A>/db/creds"
    enforce_tenant_scope(op, mount="secret", path=f"tenant-{_TENANT_A}/db/creds", read_only=True)


def test_out_of_namespace_path_is_denied() -> None:
    op = _operator(_TENANT_A)
    with pytest.raises(VaultTenantScopeError) as exc:
        enforce_tenant_scope(
            op, mount="secret", path=f"tenant-{_TENANT_B}/db/creds", read_only=True
        )
    # The message names the offending path + tenant, never a secret value.
    assert f"tenant-{_TENANT_B}/db/creds" in str(exc.value)
    assert str(_TENANT_A) in str(exc.value)


def test_sibling_prefix_does_not_satisfy_the_guard() -> None:
    """A look-alike tenant segment (no path-boundary) must not pass.

    ``secret/tenant-<A>extra/...`` shares a leading string with the
    prefix but is a different namespace — the segment-boundary match
    rejects it.
    """
    op = _operator(_TENANT_A)
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(op, mount="secret", path=f"tenant-{_TENANT_A}extra/x", read_only=True)


def test_cross_mount_request_is_denied() -> None:
    """The prefix pins the mount segment too — a different mount is denied."""
    op = _operator(_TENANT_A)
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(op, mount="other", path=f"tenant-{_TENANT_A}/db", read_only=True)


def test_empty_template_disables_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (empty prefix) is a no-op — pre-#1643 behaviour."""
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    op = _operator(_TENANT_A)
    # Any path, including a cross-tenant one, is allowed when disabled.
    enforce_tenant_scope(op, mount="secret", path=f"tenant-{_TENANT_B}/db", read_only=False)


def test_default_prefix_is_mount_pinned_per_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env override → the guard ships enforced with ``secret/tenants/{tenant_id}/`` (#1725).

    The default-on value must be **mount-pinned**: the guard matches a
    normalised ``<mount>/<path>`` candidate and the KV-v2 handlers default
    ``mount="secret"``, so a path-only ``tenants/{tenant_id}/`` would never
    match the ``secret/tenants/<id>/...`` candidate and would deny every
    legitimate per-tenant call.
    """
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    assert get_settings().vault_kv_tenant_scope_prefix == "secret/tenants/{tenant_id}/"


def test_default_config_allows_canonical_per_tenant_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the default prefix, the canonical #1723 layout call is ALLOWED.

    mount=``secret`` (the handler default) + path=``tenants/<id>/<target>``
    → candidate ``secret/tenants/<id>/<target>`` is inside the rendered
    default prefix, so no raise.
    """
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    op = _operator(_TENANT_A)
    enforce_tenant_scope(
        op, mount="secret", path=f"tenants/{_TENANT_A}/rdc-vcenter", read_only=True
    )


def test_default_config_denies_cross_tenant_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the default prefix, a cross-tenant path is DENIED."""
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    op = _operator(_TENANT_A)
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(
            op, mount="secret", path=f"tenants/{_TENANT_B}/rdc-vcenter", read_only=True
        )


def test_default_config_exempts_system_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the default prefix, the Nil-UUID system operator stays exempt."""
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    op = _operator(_SYSTEM_TENANT_ID)
    enforce_tenant_scope(op, mount="secret", path="anything/at/all", read_only=True)


def test_platform_exempt_path_is_allowed_for_real_tenant() -> None:
    """A shared platform secret (federation-proof health) bypasses the guard.

    The path is read by ``GET /api/v1/health`` under the *real* request
    operator's identity (not the system shim), so the per-tenant guard would
    otherwise deny it once enforced by default. The closed allow-list exempts
    it.
    """
    op = _operator(_TENANT_A)
    # mount "secret" (the handler default) + path "meho/test/federation".
    # Read-only — the health route only reads this path.
    enforce_tenant_scope(op, mount="secret", path="meho/test/federation", read_only=True)


def test_platform_exemption_does_not_widen_to_sibling_paths() -> None:
    """The exemption is an exact-match allow-list, not a prefix."""
    op = _operator(_TENANT_A)
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(op, mount="secret", path="meho/test/federation/escape", read_only=True)


def test_platform_exemption_is_read_only_write_is_denied() -> None:
    """A WRITE to the exempt platform path is denied — the exemption is read-only (#1725 M1).

    The health route only ever READS ``secret/meho/test/federation``. The
    closed allow-list must not bypass the guard for a mutating verb, so a
    ``put`` / ``patch`` / ``delete`` to the shared platform path under a
    non-owning operator stays tenant-scoped.
    """
    op = _operator(_TENANT_A)
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(op, mount="secret", path="meho/test/federation", read_only=False)


def test_platform_exemption_read_allowed_write_denied_same_path() -> None:
    """The same exact exempt path: read passes, write is denied (#1725 M1).

    Pins the read/write asymmetry on one path so a future refactor that
    drops the ``read_only`` gate (re-widening to all verbs) trips here.
    """
    op = _operator(_TENANT_A)
    # Read is exempt.
    enforce_tenant_scope(op, mount="secret", path="meho/test/federation", read_only=True)
    # Write to the very same path is not.
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(op, mount="secret", path="meho/test/federation", read_only=False)


def test_platform_exempt_paths_match_health_route() -> None:
    """The exempt set stays equal to the health route's federation-proof path.

    Mirrors the ``_SYSTEM_TENANT_ID`` duplication pattern: the value is
    pinned by a test rather than imported, so a drift in the health route's
    ``_FEDERATION_PROOF_PATH`` (or its mount) trips here.
    """
    from meho_backplane.api.v1.health import _FEDERATION_PROOF_PATH

    assert frozenset({f"secret/{_FEDERATION_PROOF_PATH}"}) == PLATFORM_EXEMPT_PATHS


def test_system_tenant_is_exempt() -> None:
    """The Nil-UUID system/shim operator bypasses the guard."""
    op = _operator(_SYSTEM_TENANT_ID)
    enforce_tenant_scope(op, mount="secret", path="anything/at/all", read_only=True)


def test_system_tenant_constant_matches_connector() -> None:
    """The duplicated Nil-UUID sentinel stays equal to the connector's."""
    assert UUID(int=0) == _SYSTEM_TENANT_ID


# ---------------------------------------------------------------------------
# Dispatch-level — every KV-v2 handler denies an out-of-namespace request
# ---------------------------------------------------------------------------

#: (op_id, params) for a request that targets tenant B's namespace. The
#: acting operator (tenant A) must be denied on every one. Each carries the
#: schema-required params for its op so the request reaches the handler
#: guard rather than failing param validation first.
_OUT_OF_NAMESPACE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("vault.kv.read", {"path": f"tenant-{_TENANT_B}/db/creds"}),
    ("vault.kv.list", {"path": f"tenant-{_TENANT_B}/db"}),
    ("vault.kv.versions", {"path": f"tenant-{_TENANT_B}/db/creds"}),
    ("vault.kv.put", {"path": f"tenant-{_TENANT_B}/db/creds", "data": {"k": "v"}}),
    ("vault.kv.patch", {"path": f"tenant-{_TENANT_B}/db/creds", "data": {"k": "v"}}),
    (
        "vault.kv.delete",
        {"path": f"tenant-{_TENANT_B}/db/creds", "versions": [1]},
    ),
]


@pytest.mark.parametrize(
    "op_id,params",
    _OUT_OF_NAMESPACE_CALLS,
    ids=[c[0] for c in _OUT_OF_NAMESPACE_CALLS],
)
async def test_handler_denies_cross_tenant_path(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """Tenant A requesting tenant B's path is denied on every KV-v2 handler.

    The guard raises :class:`VaultTenantScopeError` before the hvac call;
    the dispatcher wraps it into a structured ``connector_error`` whose
    ``extras.exception_class`` names the guard, distinct from a Vault-side
    403 (``VaultRoleDeniedError``) or a read-phase error.
    """
    fake = install_fake_client(monkeypatch, secret={"k": "v"})
    result = await _dispatch(op_id, params, tenant_id=_TENANT_A)

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "VaultTenantScopeError"
    # Defense-in-depth means *no* Vault round-trip happened: the guard
    # short-circuits before login.
    assert fake.auth.jwt.login_calls == []


@pytest.mark.parametrize(
    "op_id,params",
    [
        ("vault.kv.read", {"path": f"tenant-{_TENANT_A}/db/creds"}),
        ("vault.kv.list", {"path": f"tenant-{_TENANT_A}/db"}),
        ("vault.kv.versions", {"path": f"tenant-{_TENANT_A}/db/creds"}),
        (
            "vault.kv.put",
            {"path": f"tenant-{_TENANT_A}/db/creds", "data": {"k": "v"}},
        ),
        (
            "vault.kv.patch",
            {"path": f"tenant-{_TENANT_A}/db/creds", "data": {"k": "v"}},
        ),
        (
            "vault.kv.delete",
            {"path": f"tenant-{_TENANT_A}/db/creds", "versions": [1]},
        ),
    ],
    ids=[
        "read",
        "list",
        "versions",
        "put",
        "patch",
        "delete",
    ],
)
async def test_handler_allows_in_namespace_path(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """An in-namespace request is unchanged — it reaches Vault and succeeds."""
    fake = install_fake_client(monkeypatch, secret={"k": "v"}, kv_version=3)
    result = await _dispatch(op_id, params, tenant_id=_TENANT_A)

    assert result.status == "ok", result.error
    # The guard did not short-circuit: the handler logged in to Vault.
    assert fake.auth.jwt.login_calls != []


# ---------------------------------------------------------------------------
# Dispatch-level — the platform-path exemption is read-only (#1725 M1)
# ---------------------------------------------------------------------------

#: The shared platform secret the health route reads. Outside this suite's
#: ``secret/tenant-{tenant_id}/`` prefix, so it only passes the guard via
#: the read-only platform-path allow-list.
_PLATFORM_PATH = "meho/test/federation"


async def test_handler_allows_read_of_exempt_platform_path(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """``vault.kv.read`` of the exempt platform path is allowed for a real tenant."""
    fake = install_fake_client(monkeypatch, secret={"k": "v"}, kv_version=3)
    result = await _dispatch("vault.kv.read", {"path": _PLATFORM_PATH}, tenant_id=_TENANT_A)

    assert result.status == "ok", result.error
    # The read reached Vault — the read-only platform exemption applied.
    assert fake.auth.jwt.login_calls != []


@pytest.mark.parametrize(
    "op_id,params",
    [
        ("vault.kv.put", {"path": _PLATFORM_PATH, "data": {"k": "v"}}),
        ("vault.kv.patch", {"path": _PLATFORM_PATH, "data": {"k": "v"}}),
        ("vault.kv.delete", {"path": _PLATFORM_PATH, "versions": [1]}),
    ],
    ids=["put", "patch", "delete"],
)
async def test_handler_denies_write_to_exempt_platform_path(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """A WRITE to the exempt platform path is denied — read-only exemption (#1725 M1).

    The platform-path allow-list bypasses the guard only for read-only ops.
    A mutating ``put`` / ``patch`` / ``delete`` to the shared platform path
    under a non-owning operator must still be tenant-scoped, so the guard
    short-circuits before any Vault round-trip.
    """
    fake = install_fake_client(monkeypatch, secret={"k": "v"})
    result = await _dispatch(op_id, params, tenant_id=_TENANT_A)

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultTenantScopeError"
    # Defense-in-depth: no Vault round-trip — the guard denied before login.
    assert fake.auth.jwt.login_calls == []
