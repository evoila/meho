# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A tenant with its own Vault login role always gets the tenant folder check.

The folder check runs for a tenant that has an entry in
``VAULT_OIDC_ROLE_BY_TENANT`` even when ``VAULT_KV_TENANT_SCOPE_PREFIX`` is
empty. It covers the check runner's reads, a person's ``vault.kv.*`` call,
the run-time read of a target's secret, and the write of a target's
``secret_ref``. A tenant without an entry, and an install with a non-empty
setting, behave as before. The tests use the in-process Vault fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from meho_backplane.api.v1.targets import _enforce_secret_ref_tenant_scope
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import VaultCredentialBackend
from meho_backplane.connectors.vault.tenant_scope import (
    VaultTenantScopeError,
    count_tenants_with_own_vault_role,
    effective_tenant_scope_template,
    enforce_tenant_scope,
)
from meho_backplane.main import _advise_vault_tenant_scope_unenforced
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_TENANT_T = UUID("0000aaaa-0000-0000-0000-000000000001")
_TENANT_U = UUID("0000bbbb-0000-0000-0000-000000000002")
_OWN = f"tenants/{_TENANT_T}"
_OTHER = f"tenants/{_TENANT_U}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    monkeypatch.setenv("VAULT_OIDC_ROLE_BY_TENANT", f"{_TENANT_T}=meho-tenant-t")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant: UUID = _TENANT_T, *, check_runner: bool = False) -> Operator:
    return Operator(
        sub="folder-check-test",
        name=None,
        email=None,
        raw_jwt="op.jwt",
        tenant_id=tenant,
        tenant_role=TenantRole.OPERATOR,
        check_runner_dispatch=check_runner,
    )


def _enforce(op: Operator, path: str, *, read_only: bool = True) -> None:
    enforce_tenant_scope(op, mount="secret", path=path, read_only=read_only)


# --- check runner ----------------------------------------------------------


async def test_runner_read_outside_folder_is_refused_before_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "other-value"})
    with pytest.raises(VaultTenantScopeError):
        await VaultCredentialBackend().load_secret_data(
            "meho/x", _operator(check_runner=True), target_name="vc-01"
        )
    assert fake.auth.jwt.login_calls == []


async def test_runner_read_inside_folder_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "own-value"})
    data = await VaultCredentialBackend().load_secret_data(
        f"{_OWN}/x", _operator(check_runner=True), target_name="vc-01"
    )
    assert data == {"password": "own-value"}
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == f"{_OWN}/x"


# --- target write ----------------------------------------------------------


@pytest.mark.parametrize("ref", ["meho/x", f"{_OTHER}/x", f"vault:{_OTHER}/x"])
def test_target_write_outside_folder_is_422(ref: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _enforce_secret_ref_tenant_scope(ref, _operator(), "vc-01")
    assert exc.value.status_code == 422
    assert exc.value.detail["kind"] == "secret_ref_outside_tenant_scope"  # type: ignore[index]


def test_target_write_inside_folder_passes() -> None:
    _enforce_secret_ref_tenant_scope(f"{_OWN}/x", _operator(), "vc-01")


# --- a person --------------------------------------------------------------


def test_person_kv_call_on_another_tenants_path_is_refused() -> None:
    with pytest.raises(VaultTenantScopeError):
        _enforce(_operator(), f"{_OTHER}/x")
    _enforce(_operator(), f"{_OWN}/x")


async def test_person_target_read_outside_folder_is_refused_before_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "other-value"})
    with pytest.raises(VaultTenantScopeError):
        await VaultCredentialBackend().load_secret_data(
            "targets/vc-01", _operator(), target_name="vc-01"
        )
    assert fake.auth.jwt.login_calls == []


async def test_person_target_read_inside_folder_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "own-value"})
    data = await VaultCredentialBackend().load_secret_data(
        f"{_OWN}/vc-01", _operator(), target_name="vc-01"
    )
    assert data == {"password": "own-value"}
    assert len(fake.secrets.kv.v2.read_calls) == 1


# --- tenant without an entry ----------------------------------------------


async def test_tenant_without_role_entry_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    other = _operator(_TENANT_U)
    assert effective_tenant_scope_template(other) == ""
    _enforce(other, "meho/x")
    _enforce_secret_ref_tenant_scope("meho/x", other, "vc-01")
    fake = install_fake_client(monkeypatch, secret={"password": "v"})
    data = await VaultCredentialBackend().load_secret_data(
        "targets/vc-01", other, target_name="vc-01"
    )
    assert data == {"password": "v"}
    assert len(fake.secrets.kv.v2.read_calls) == 1


# --- non-empty setting -----------------------------------------------------


def test_non_empty_setting_wins_for_both_tenants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "secret/tenant-{tenant_id}/")
    get_settings.cache_clear()
    for tenant in (_TENANT_T, _TENANT_U):
        op = _operator(tenant)
        assert effective_tenant_scope_template(op) == "secret/tenant-{tenant_id}/"
        _enforce(op, f"tenant-{tenant}/x")
        with pytest.raises(VaultTenantScopeError):
            _enforce(op, f"{_OWN}/x")


# --- exceptions that stay --------------------------------------------------


def test_health_read_still_works_for_tenant_with_role() -> None:
    _enforce(_operator(), "meho/test/federation", read_only=True)
    with pytest.raises(VaultTenantScopeError):
        _enforce(_operator(), "meho/test/federation", read_only=False)


def test_plain_path_rule_still_runs() -> None:
    with pytest.raises(VaultTenantScopeError):
        _enforce(_operator(_TENANT_U), "a/../b")


def test_count_of_tenants_with_own_role(monkeypatch: pytest.MonkeyPatch) -> None:
    assert count_tenants_with_own_vault_role(get_settings().vault_oidc_role_by_tenant) == 1
    assert count_tenants_with_own_vault_role("") == 0


def test_tenant_listed_twice_counts_once() -> None:
    twice = f"{_TENANT_T}=meho-tenant-t, {str(_TENANT_T).upper()}=meho-tenant-t2"
    assert count_tenants_with_own_vault_role(twice) == 1
    assert count_tenants_with_own_vault_role(f"{twice},{_TENANT_U}=meho-tenant-u") == 2


def test_startup_logs_count_of_tenants_with_own_role() -> None:
    with capture_logs() as captured:
        _advise_vault_tenant_scope_unenforced()
    lines = [e for e in captured if e.get("event") == "vault_tenant_scope_tenants_with_own_role"]
    assert [e["count"] for e in lines] == [1]
