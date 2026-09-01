# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-target Vault role/auth-mount resolution (#3274).

Pins the ``Target``-side contract frozen with the lab
(``claude-rdc-hetzner-dc#2814`` / PR ``#2815``): a vault target advertising
``extras["vault_role"]`` (optionally ``extras["vault_mount"]``) selects that
role at login; anything else falls through to the settings-global role +
mount byte-for-byte. The live ``rdc-vault-teardown`` target ships
``version=null`` / ``secret_ref=null``, so the resolver is exercised against
that exact shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vault.target_auth import (
    VaultTargetAuth,
    resolve_vault_target_auth,
    vault_client_for_target,
)
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target

from ._vault_fakes import install_fake_vault


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var the Settings model reads (cached per-process)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_CHECK_RUNNER_ROLE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_vault_target(**overrides: Any) -> Target:
    """Build a ``Target`` in the live ``rdc-vault-teardown`` shape.

    ``product="vault"``, ``version=None`` (resolves via the connector's
    wildcard registration), ``secret_ref=None`` (JWT-federated — no stored
    per-target credential). ``extras`` is the only per-target auth carrier.
    """
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "rdc-vault-teardown",
        "aliases": [],
        "product": "vault",
        "version": None,
        "host": "vault.rdc.internal",
        "port": 8200,
        "fqdn": "vault.rdc.internal",
        "secret_ref": None,
        "auth_model": AuthModel.SHARED_SERVICE_ACCOUNT,
        "vpn_required": True,
        "extras": {},
        "notes": None,
        "fingerprint": None,
        "preferred_impl_id": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Target(**defaults)


def _make_operator(jwt: str = "op-jwt") -> Operator:
    return Operator(
        sub="op-1",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# resolve_vault_target_auth
# ---------------------------------------------------------------------------


def test_resolve_none_target_is_no_override() -> None:
    """A ``None`` target (connector-id-routed call) selects no override."""
    assert resolve_vault_target_auth(None) == VaultTargetAuth(None, None)


def test_resolve_role_only() -> None:
    """``extras["vault_role"]`` alone selects the role; mount stays settings-global."""
    target = _make_vault_target(extras={"vault_role": "meho-teardown"})
    assert resolve_vault_target_auth(target) == VaultTargetAuth("meho-teardown", None)


def test_resolve_role_and_mount_live_teardown_shape() -> None:
    """The live ``rdc-vault-teardown`` shape resolves both role and auth mount.

    version=None, secret_ref=None, extras carry role ``meho-teardown`` on the
    ``jwt-meho`` auth mount — the exact registered target this ships for.
    """
    target = _make_vault_target(
        version=None,
        secret_ref=None,
        extras={"vault_role": "meho-teardown", "vault_mount": "jwt-meho"},
    )
    assert resolve_vault_target_auth(target) == VaultTargetAuth("meho-teardown", "jwt-meho")


def test_resolve_non_vault_product_ignores_role() -> None:
    """The product gate: a non-vault target's stray ``vault_role`` is ignored."""
    target = _make_vault_target(product="vsphere", extras={"vault_role": "meho-teardown"})
    assert resolve_vault_target_auth(target) == VaultTargetAuth(None, None)


@pytest.mark.parametrize("role_value", ["", "   ", 123, None, {"nested": "x"}])
def test_resolve_blank_or_non_string_role_is_no_override(role_value: Any) -> None:
    """Blank / whitespace / non-string ``vault_role`` normalises to no override.

    An empty string does not *name* a role (it is not a denial, just no
    selection) — matching the blank-``VAULT_CHECK_RUNNER_ROLE`` normalisation.
    """
    target = _make_vault_target(extras={"vault_role": role_value})
    assert resolve_vault_target_auth(target) == VaultTargetAuth(None, None)


def test_resolve_no_extras_keys_is_no_override() -> None:
    """A vault target with empty extras keeps today's settings-global login."""
    assert resolve_vault_target_auth(_make_vault_target()) == VaultTargetAuth(None, None)


# ---------------------------------------------------------------------------
# vault_client_for_target — resolution threaded into the login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vault_client_for_target_threads_role_and_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown target logs in under its role on its auth mount."""
    get_settings.cache_clear()
    fake = install_fake_vault(monkeypatch)
    target = _make_vault_target(
        extras={"vault_role": "meho-teardown", "vault_mount": "jwt-meho"},
    )

    async with vault_client_for_target(_make_operator(jwt="op-jwt"), target):
        pass

    assert fake.auth.jwt.login_calls == [
        {"role": "meho-teardown", "jwt": "op-jwt", "path": "jwt-meho"},
    ]


@pytest.mark.asyncio
async def test_vault_client_for_target_none_target_uses_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` target keeps the settings-global role + mount byte-for-byte."""
    get_settings.cache_clear()
    fake = install_fake_vault(monkeypatch)

    async with vault_client_for_target(_make_operator(jwt="op-jwt"), None):
        pass

    assert fake.auth.jwt.login_calls == [
        {"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"},
    ]


@pytest.mark.asyncio
async def test_kv_delete_dispatches_under_target_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a governed ``vault.kv.delete`` runs under the target's role.

    Drives the real handler end-to-end — resolution → login — proving the
    governed teardown's soft-delete authenticates as ``meho-teardown`` on the
    ``jwt-meho`` mount, not the shared ``meho-mcp`` identity.
    """
    from meho_backplane.connectors.vault.ops import vault_kv_delete

    # Disable the orthogonal tenant-scope guard (#1643, its own tests) so this
    # test isolates the role-selection contract, not the path allow-list.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    fake = install_fake_vault(monkeypatch)
    target = _make_vault_target(
        extras={"vault_role": "meho-teardown", "vault_mount": "jwt-meho"},
    )

    result = await vault_kv_delete(
        _make_operator(jwt="op-jwt"),
        target,
        {"path": "meho/scratch", "versions": [1]},
    )

    assert result == {"deleted_versions": [1]}
    assert fake.auth.jwt.login_calls == [
        {"role": "meho-teardown", "jwt": "op-jwt", "path": "jwt-meho"},
    ]


@pytest.mark.asyncio
async def test_write_preflight_runs_under_target_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: the park-time capability preflight probes under the resolved role.

    So the approval banner reflects the role that will actually execute the
    write, not the settings-global ``meho-mcp`` identity.
    """
    from meho_backplane.connectors.vault.ops import vault_kv_write_capability_preflight

    get_settings.cache_clear()
    fake = install_fake_vault(monkeypatch)
    fake.sys.capabilities_by_path = {"secret/data/meho/x": ["create", "update"]}
    target = _make_vault_target(
        extras={"vault_role": "meho-teardown", "vault_mount": "jwt-meho"},
    )

    result = await vault_kv_write_capability_preflight(
        _make_operator(jwt="op-jwt"),
        "vault.kv.put",
        {"path": "meho/x"},
        target=target,
    )

    assert result is not None
    assert result["will_be_denied"] is False
    assert fake.auth.jwt.login_calls == [
        {"role": "meho-teardown", "jwt": "op-jwt", "path": "jwt-meho"},
    ]
