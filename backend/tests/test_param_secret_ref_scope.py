# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret refs named in operation parameters are checked before any read.

What these tests pin:

* :func:`~meho_backplane.connectors._shared.vault_creds.check_param_secret_ref`
  lets a Vault path through only when it sits in the caller's tenant
  subtree, with no ``.`` / ``..`` segment, no ``%`` and no backslash; a
  ``gsm:`` secret only when its project is in ``GSM_PARAM_ALLOWED_PROJECTS``
  (an empty list refuses all); any other store kind never.
* A refused ref stops the operation before Vault (or Google) is called,
  for every family of operations that takes a secret ref in its params.
* The check runner does not start with a service principal on a Vault
  install unless ``VAULT_CHECK_RUNNER_ROLE`` names a role of its own, and
  each of its Vault reads passes the tenant path check.
* A target's ``secret_ref`` with a ``..`` segment is refused at write time.

The tests run against the in-process Vault fake; no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared import credential_backend as cb
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    SecretRefScopeError,
    VaultCredentialBackend,
    check_param_secret_ref,
    load_param_secret_data,
)
from meho_backplane.connectors.vault.tenant_scope import (
    VaultTenantScopeError,
    enforce_tenant_scope,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_TENANT_A = UUID("0000aaaa-0000-0000-0000-000000000001")
_TENANT_B = UUID("0000bbbb-0000-0000-0000-000000000002")
_OWN = f"tenants/{_TENANT_A}"
_OTHER = f"tenants/{_TENANT_B}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env Settings reads; the tenant prefix stays at its default."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SENSOR_RUNNER_ENABLED", "false")
    for name in (
        "VAULT_NAMESPACE",
        "VAULT_KV_TENANT_SCOPE_PREFIX",
        "CREDENTIAL_BACKEND",
        "GSM_PARAM_ALLOWED_PROJECTS",
        "CHECK_RUNNER_CLIENT_ID",
        "CHECK_RUNNER_CLIENT_SECRET",
        "VAULT_CHECK_RUNNER_ROLE",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _operator(
    tenant: UUID = _TENANT_A,
    *,
    check_runner: bool = False,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    return Operator(
        sub="secret-scope-test",
        name=None,
        email=None,
        raw_jwt="op.jwt",
        tenant_id=tenant,
        tenant_role=role,
        check_runner_dispatch=check_runner,
    )


def _assert_param_read_result(exc: SecretRefScopeError, param: str, full_path: str) -> None:
    """The dispatcher's answer for *exc* names *param* and never the full path."""
    from meho_backplane.operations._errors import result_vault_credentials_read_error

    result = result_vault_credentials_read_error("op", exc, 1.0)
    assert result.extras["error_code"] == "vault_credentials_read_error"
    assert result.extras["param"] == param
    dumped = json.dumps(result.model_dump(mode="json"))
    assert full_path not in dumped
    assert str(_TENANT_B) not in dumped


@dataclass
class _Target:
    name: str = "scope-target"
    host: str = "scope-target.test.invalid"
    secret_ref: str | None = None


class _RecordingGsmBackend:
    """Stands in for the gsm backend and records every read."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def load_secret_data(
        self, secret_ref: str, operator: Operator, *, target_name: str, mount: str
    ) -> dict[str, object]:
        del operator, target_name, mount
        self.calls.append(secret_ref)
        return {"password": "gsm-value"}


# ---------------------------------------------------------------------------
# check_param_secret_ref: Vault paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        f"{_OWN}/app/db",
        f"vault:{_OWN}/app/db",
        f"  {_OWN}/app/db  ",
    ],
)
def test_vault_ref_in_own_tenant_passes(ref: str) -> None:
    check_param_secret_ref(_operator(), ref, param="password_secret_ref")


_REFS_THAT_COULD_MOVE = [
    f"{_OWN}/../{_TENANT_B}/app",
    f"{_OWN}/app/../../{_TENANT_B}/app",
    f"{_OWN}/./app",
    f"{_OWN}/%2e%2e/{_TENANT_B}/app",
    f"{_OWN}/%2E%2E%2F{_TENANT_B}/app",
    f"{_OWN}%2f..%2f{_TENANT_B}/app",
    f"{_OWN}/..\\{_TENANT_B}\\app",
]


@pytest.mark.parametrize(
    "ref",
    [
        f"{_OTHER}/app/db",
        f"vault:{_OTHER}/app/db",
        "meho/shared/app",
        f"{_OWN}-sibling/app",
    ],
)
def test_vault_ref_outside_own_tenant_is_refused(ref: str) -> None:
    with pytest.raises(SecretRefScopeError) as exc:
        check_param_secret_ref(_operator(), ref, param="password_secret_ref")
    assert exc.value.param == "password_secret_ref"
    assert exc.value.cause_class == "SecretRefScopeError"
    # The message names the param and the ref's first segment only.
    assert str(_TENANT_B) not in str(exc.value)
    assert "outside the caller's tenant" in str(exc.value)


@pytest.mark.parametrize("ref", _REFS_THAT_COULD_MOVE)
def test_vault_ref_that_could_move_is_refused(ref: str) -> None:
    """A ``.`` / ``..`` segment, a ``%`` or a backslash is refused, and the reason says so."""
    with pytest.raises(SecretRefScopeError) as exc:
        check_param_secret_ref(_operator(), ref, param="password_secret_ref")
    assert exc.value.param == "password_secret_ref"
    assert str(_TENANT_B) not in str(exc.value)
    assert "a '.' or '..' segment, a '%' or a backslash" in str(exc.value)
    assert "outside the caller's tenant" not in str(exc.value)


@pytest.mark.parametrize("mount", ["other-mount", f"secret/{_OWN}/..", "secret%2f"])
def test_vault_mount_outside_own_tenant_is_refused(mount: str) -> None:
    with pytest.raises(SecretRefScopeError):
        check_param_secret_ref(_operator(), f"{_OWN}/app", param="data_secret_ref", mount=mount)


def test_vault_ref_is_not_checked_when_the_tenant_prefix_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``VAULT_KV_TENANT_SCOPE_PREFIX=""`` turns the tenant prefix check off: a plain ref passes."""
    _set_env(monkeypatch, VAULT_KV_TENANT_SCOPE_PREFIX="")
    check_param_secret_ref(_operator(), f"{_OTHER}/app/db", param="password_secret_ref")


@pytest.mark.parametrize("ref", _REFS_THAT_COULD_MOVE)
def test_vault_ref_that_could_move_is_refused_when_the_tenant_prefix_is_off(
    monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """With an empty prefix, a ``.`` / ``..`` segment, a ``%`` or a backslash is still refused."""
    _set_env(monkeypatch, VAULT_KV_TENANT_SCOPE_PREFIX="")
    with pytest.raises(SecretRefScopeError) as exc:
        check_param_secret_ref(_operator(), ref, param="password_secret_ref")
    assert "a '.' or '..' segment, a '%' or a backslash" in str(exc.value)
    assert "outside the caller's tenant" not in str(exc.value)


def test_empty_ref_and_system_operator_pass() -> None:
    check_param_secret_ref(_operator(), "   ", param="password_secret_ref")
    check_param_secret_ref(None, f"{_OTHER}/app/db", param="secret_env")


def test_shared_check_refuses_dot_segments_for_kv_ops_too() -> None:
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(
            _operator(), mount="secret", path=f"{_OWN}/../{_TENANT_B}/x", read_only=True
        )
    enforce_tenant_scope(_operator(), mount="secret", path=f"{_OWN}/x", read_only=True)


# ---------------------------------------------------------------------------
# check_param_secret_ref: Google Secret Manager and other stores
# ---------------------------------------------------------------------------


def test_gsm_ref_is_refused_when_no_project_is_allowed() -> None:
    with pytest.raises(SecretRefScopeError) as exc:
        check_param_secret_ref(_operator(), "gsm:proj-a/db#password", param="password_secret_ref")
    assert "GSM_PARAM_ALLOWED_PROJECTS" in str(exc.value)


def test_gsm_ref_passes_only_for_an_allowed_project(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, GSM_PARAM_ALLOWED_PROJECTS=" proj-a , proj-b ")
    check_param_secret_ref(_operator(), "gsm:proj-a/db#password", param="password_secret_ref")
    check_param_secret_ref(_operator(), "gsm:proj-b/db/versions/3", param="password_secret_ref")
    with pytest.raises(SecretRefScopeError):
        check_param_secret_ref(_operator(), "gsm:proj-c/db", param="password_secret_ref")


def test_schemeless_ref_on_a_gsm_install_follows_the_gsm_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, CREDENTIAL_BACKEND="gsm")
    with pytest.raises(SecretRefScopeError):
        check_param_secret_ref(_operator(), "proj-a/db", param="password_secret_ref")
    _set_env(monkeypatch, GSM_PARAM_ALLOWED_PROJECTS="proj-a")
    check_param_secret_ref(_operator(), "proj-a/db", param="password_secret_ref")


def test_other_store_kinds_are_refused() -> None:
    with pytest.raises(SecretRefScopeError) as exc:
        check_param_secret_ref(_operator(), "wrapped:s.token", param="password_secret_ref")
    assert "'wrapped'" in str(exc.value)


# ---------------------------------------------------------------------------
# The shared param reader: refused before any store call
# ---------------------------------------------------------------------------


async def test_param_reader_refuses_before_any_vault_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "other-tenant-value"})
    with pytest.raises(SecretRefScopeError):
        await load_param_secret_data(
            _Target(secret_ref=f"{_OWN}/../{_TENANT_B}/db"),
            _operator(),
            param="password_secret_ref",
        )
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.read_calls == []


async def test_param_reader_reads_a_ref_in_own_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "own-value"})
    data = await load_param_secret_data(
        _Target(secret_ref=f"{_OWN}/db"), _operator(), param="password_secret_ref"
    )
    assert data == {"password": "own-value"}
    assert fake.secrets.kv.v2.read_calls[-1] == {"path": f"{_OWN}/db", "mount_point": "secret"}


async def test_param_reader_refuses_gsm_before_any_google_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _RecordingGsmBackend()
    monkeypatch.setitem(cb.CREDENTIAL_BACKEND_REGISTRY, "gsm", backend)
    with pytest.raises(SecretRefScopeError):
        await load_param_secret_data(
            _Target(secret_ref="gsm:proj-a/db#password"), _operator(), param="password_secret_ref"
        )
    assert backend.calls == []


def test_refusal_maps_to_the_param_read_error_result() -> None:
    from meho_backplane.operations._errors import result_vault_credentials_read_error

    exc = SecretRefScopeError(
        param="password_secret_ref",
        secret_ref=f"{_OTHER}/db",
        reason="the path is outside the caller's tenant",
    )
    result = result_vault_credentials_read_error("keycloak.user.create", exc, 1.0)
    assert result.status == "error"
    assert result.extras["error_code"] == "vault_credentials_read_error"
    assert result.extras["param"] == "password_secret_ref"
    assert result.extras["secret_ref_prefix"] == "tenants/…"
    assert result.extras["cause_class"] == "SecretRefScopeError"
    assert str(_TENANT_B) not in (result.error or "")


# ---------------------------------------------------------------------------
# One test per family of operations that takes a secret ref in its params
# ---------------------------------------------------------------------------


async def test_linux_script_run_secret_env_outside_tenant_is_refused_before_read() -> None:
    from meho_backplane.connectors.linux import LinuxSshConnector
    from meho_backplane.connectors.linux.ops_write import _resolve_secret_env

    path = f"{_OWN}/../{_TENANT_B}/db"
    connector = LinuxSshConnector()
    connector._resolve_secret = AsyncMock(return_value={"v": "x"})  # type: ignore[method-assign]
    with pytest.raises(SecretRefScopeError) as exc:
        await _resolve_secret_env(connector, _Target(), _operator(), {"DB_PASS": (path, "v")})
    connector._resolve_secret.assert_not_awaited()
    _assert_param_read_result(exc.value, "secret_env", path)


async def test_linux_script_run_secret_env_in_own_tenant_is_read() -> None:
    from meho_backplane.connectors.linux import LinuxSshConnector
    from meho_backplane.connectors.linux.ops_write import _resolve_secret_env

    connector = LinuxSshConnector()
    connector._resolve_secret = AsyncMock(return_value={"v": "x"})  # type: ignore[method-assign]
    resolved = await _resolve_secret_env(
        connector, _Target(), _operator(), {"DB_PASS": (f"{_OWN}/db", "v")}
    )
    assert resolved == {"DB_PASS": "x"}


async def test_msad_password_ref_outside_tenant_is_refused_before_read() -> None:
    from meho_backplane.connectors.msad import MsadConnector
    from meho_backplane.connectors.msad.ops_password import _read_password

    path = f"{_OTHER}/ad/svc"
    connector = MsadConnector()
    connector._resolve_secret = AsyncMock(return_value={"pw": "x"})  # type: ignore[method-assign]
    with pytest.raises(SecretRefScopeError) as exc:
        await _read_password(connector, _Target(), _operator(), (path, "pw"))
    connector._resolve_secret.assert_not_awaited()
    _assert_param_read_result(exc.value, "password_ref", path)


@pytest.fixture
async def _ssh_targets() -> AsyncIterator[None]:
    """Register the Linux and AD ops and seed one target of each in tenant A."""
    from meho_backplane.connectors.linux import LinuxSshConnector
    from meho_backplane.connectors.msad import MsadConnector
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM
    from meho_backplane.operations import reset_dispatcher_caches
    from meho_backplane.operations.dispatcher import set_default_reducer
    from meho_backplane.operations.reducer import PassThroughReducer

    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    await LinuxSshConnector.register_operations()
    await MsadConnector.register_operations()
    async with get_sessionmaker()() as session:
        for name, product, version in (
            ("scope-linux", "linux", "1.x"),
            ("scope-ad", "msad", "2022.x"),
        ):
            session.add(
                TargetORM(
                    tenant_id=_TENANT_A,
                    name=name,
                    aliases=[],
                    product=product,
                    host=f"{name}.test.invalid",
                    port=22,
                    fqdn=None,
                    secret_ref=f"{_OWN}/{name}",
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={},
                    fingerprint={"version": version},
                    notes="seeded by test_param_secret_ref_scope",
                )
            )
        await session.commit()
    yield
    reset_dispatcher_caches()


@pytest.mark.parametrize(
    ("connector_id", "op_id", "target_name", "params", "param", "path"),
    [
        (
            "linux-ssh-1.x",
            "linux.script.run",
            "scope-linux",
            {"script": "echo hi", "secret_env": {"DB_PASS": f"{_OTHER}/app/db#cred"}},
            "secret_env",
            f"{_OTHER}/app/db",
        ),
        (
            "msad-ssh-2022.x",
            "msad.user.set-password",
            "scope-ad",
            {"identity": "jdoe", "password_ref": f"{_OTHER}/ad/jdoe#password"},
            "password_ref",
            f"{_OTHER}/ad/jdoe",
        ),
    ],
)
async def test_ssh_param_ref_outside_tenant_gives_param_read_error(
    _ssh_targets: None,
    monkeypatch: pytest.MonkeyPatch,
    connector_id: str,
    op_id: str,
    target_name: str,
    params: dict[str, Any],
    param: str,
    path: str,
) -> None:
    """The approved run stops with vault_credentials_read_error and no full path."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    fake = install_fake_client(monkeypatch, secret={"cred": "x", "password": "x"})
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_A, target_name)
    result = await dispatch(
        operator=_operator(role=TenantRole.TENANT_ADMIN),
        connector_id=connector_id,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "error", dumped
    assert dumped["extras"]["error_code"] == "vault_credentials_read_error"
    assert dumped["extras"]["param"] == param
    assert dumped["extras"]["cause_class"] == "SecretRefScopeError"
    assert path not in json.dumps(dumped)
    assert fake.auth.jwt.login_calls == []


async def test_keycloak_password_ref_outside_tenant_is_refused_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.keycloak.ops_write import _read_password_from_vault

    fake = install_fake_client(monkeypatch, secret={"password": "other-tenant-value"})
    with pytest.raises(SecretRefScopeError):
        await _read_password_from_vault(
            _operator(),
            _Target(),
            {"password_secret_ref": f"{_OWN}/%2e%2e/{_TENANT_B}/kc-user"},
        )
    assert fake.auth.jwt.login_calls == []


async def test_keycloak_password_mount_outside_tenant_is_refused_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.keycloak.ops_write import _read_password_from_vault

    fake = install_fake_client(monkeypatch, secret={"password": "other-mount-value"})
    with pytest.raises(SecretRefScopeError):
        await _read_password_from_vault(
            _operator(),
            _Target(),
            {"password_secret_ref": f"{_OWN}/kc-user", "password_secret_mount": "kv-shared"},
        )
    assert fake.auth.jwt.login_calls == []


async def test_vcf_installer_secret_ref_outside_tenant_is_refused_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.vcf_installer._secret_refs import read_secret_ref

    fake = install_fake_client(monkeypatch, secret={"password": "other-tenant-value"})
    with pytest.raises(SecretRefScopeError):
        await read_secret_ref(
            _operator(),
            _Target(),
            {"secret_ref": f"{_OTHER}/depot"},
            param="depot.password_secret_ref",
        )
    assert fake.auth.jwt.login_calls == []


async def test_k8s_secret_refs_outside_tenant_are_refused_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.kubernetes.ops_write_dangerous import _resolve_secret_refs

    fake = install_fake_client(monkeypatch, secret={"token": "other-tenant-value"})
    with pytest.raises(SecretRefScopeError) as exc:
        await _resolve_secret_refs(
            _operator(),
            _Target(),
            {"token": f"{_OWN}/../{_TENANT_B}/app#token"},
            mount=DEFAULT_KV_MOUNT,
            param="string_data_secret_ref",
        )
    assert exc.value.param == "string_data_secret_ref"
    assert fake.auth.jwt.login_calls == []


async def test_vcf_automation_password_ref_outside_tenant_is_refused_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.vcf_automation._lookups import read_password

    fake = install_fake_client(monkeypatch, secret={"password": "other-tenant-value"})
    with pytest.raises(SecretRefScopeError):
        await read_password(_operator(), _Target(), {"password_secret_ref": f"{_OTHER}/org-admin"})
    assert fake.auth.jwt.login_calls == []


# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------


def _set_runner_principal(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    _set_env(
        monkeypatch,
        CHECK_RUNNER_CLIENT_ID="meho-check-runner",
        CHECK_RUNNER_CLIENT_SECRET="runner-client-secret",  # gitleaks:allow -- synthetic
        **extra,
    )


def test_runner_refuses_a_principal_on_vault_without_its_own_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.checks.runner import sensor_runner_refusal

    _set_runner_principal(monkeypatch)
    reason = sensor_runner_refusal()
    assert reason is not None
    assert "VAULT_CHECK_RUNNER_ROLE" in reason


def test_runner_refuses_when_its_role_is_the_shared_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.checks.runner import sensor_runner_refusal

    _set_runner_principal(monkeypatch, VAULT_CHECK_RUNNER_ROLE="meho-mcp")
    assert sensor_runner_refusal() is not None


def test_runner_starts_with_a_role_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.checks.runner import sensor_runner_refusal

    _set_runner_principal(monkeypatch, VAULT_CHECK_RUNNER_ROLE="meho-check-runner")
    assert sensor_runner_refusal() is None


def test_runner_starts_without_a_principal_or_without_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.checks.runner import sensor_runner_refusal

    assert sensor_runner_refusal() is None
    _set_runner_principal(monkeypatch)
    monkeypatch.delenv("VAULT_ADDR")
    get_settings.cache_clear()
    assert sensor_runner_refusal() is None


async def test_runner_loop_does_not_start_when_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.checks import runner

    _set_runner_principal(monkeypatch)
    tick = AsyncMock(return_value=0)
    monkeypatch.setattr(runner, "run_one_sensor_tick", tick)
    with capture_logs() as logs:
        await asyncio.wait_for(runner._runner_loop(), timeout=2)
    tick.assert_not_awaited()
    refused = [entry for entry in logs if entry["event"] == "sensor_runner_refused"]
    assert len(refused) == 1
    assert refused[0]["log_level"] == "error"


async def test_runner_vault_read_outside_sensor_tenant_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "other-tenant-value"})
    with pytest.raises(VaultTenantScopeError):
        await VaultCredentialBackend().load_secret_data(
            f"{_OTHER}/vc-01", _operator(check_runner=True), target_name="vc-01"
        )
    assert fake.auth.jwt.login_calls == []


async def test_runner_vault_read_inside_sensor_tenant_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": "own-value"})
    data = await VaultCredentialBackend().load_secret_data(
        f"{_OWN}/vc-01", _operator(check_runner=True), target_name="vc-01"
    )
    assert data == {"password": "own-value"}
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == f"{_OWN}/vc-01"


async def test_person_target_read_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A person's read of a target's own secret_ref keeps its old behaviour."""
    fake = install_fake_client(monkeypatch, secret={"password": "target-value"})
    data = await VaultCredentialBackend().load_secret_data(
        "targets/vc-01", _operator(), target_name="vc-01"
    )
    assert data == {"password": "target-value"}
    assert len(fake.secrets.kv.v2.read_calls) == 1


# ---------------------------------------------------------------------------
# Target write: a ref with dot segments is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref", [f"{_OWN}/../{_TENANT_B}/vc-01", f"{_OWN}/%2e%2e/{_TENANT_B}/vc-01"]
)
def test_target_secret_ref_with_dot_segments_is_refused(ref: str) -> None:
    from meho_backplane.api.v1.targets import _enforce_secret_ref_tenant_scope

    with pytest.raises(HTTPException) as exc:
        _enforce_secret_ref_tenant_scope(ref, _operator(), "vc-01")
    assert exc.value.status_code == 422
    detail: Any = exc.value.detail
    assert detail["kind"] == "secret_ref_outside_tenant_scope"
    assert "a '.' or '..' segment, a '%' or a backslash" in detail["message"]
    _enforce_secret_ref_tenant_scope(f"{_OWN}/vc-01", _operator(), "vc-01")


@pytest.mark.parametrize("ref", ["vault:../vc-01", "vault:./vc-01", f"vault:{_OWN}/../vc-01"])
def test_target_secret_ref_with_vault_scheme_and_dot_segment_is_refused(ref: str) -> None:
    """The gate checks the path after the ``vault:`` scheme, as the credential read does."""
    from meho_backplane.api.v1.targets import _enforce_secret_ref_tenant_scope

    with pytest.raises(HTTPException) as exc:
        _enforce_secret_ref_tenant_scope(ref, _operator(), "vc-01")
    assert exc.value.status_code == 422
    detail: Any = exc.value.detail
    assert detail["kind"] == "secret_ref_outside_tenant_scope"
    assert "a '.' or '..' segment, a '%' or a backslash" in detail["message"]
    _enforce_secret_ref_tenant_scope(f"vault:{_OWN}/vc-01", _operator(), "vc-01")


@pytest.mark.parametrize("ref", ["vault:../vc-01", "vault:./vc-01"])
def test_target_secret_ref_with_vault_scheme_and_dot_segment_is_refused_when_the_prefix_is_off(
    monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """With an empty prefix, a ``vault:`` scheme does not hide a ``.`` or ``..`` segment."""
    from meho_backplane.api.v1.targets import _enforce_secret_ref_tenant_scope

    _set_env(monkeypatch, VAULT_KV_TENANT_SCOPE_PREFIX="")
    with pytest.raises(HTTPException) as exc:
        _enforce_secret_ref_tenant_scope(ref, _operator(), "vc-01")
    assert exc.value.status_code == 422
    detail: Any = exc.value.detail
    assert detail["kind"] == "secret_ref_outside_tenant_scope"
    assert detail["tenant_prefix"] == ""
    assert "a '.' or '..' segment, a '%' or a backslash" in detail["message"]
    _enforce_secret_ref_tenant_scope("vault:vc-01", _operator(), "vc-01")


@pytest.mark.parametrize(
    "ref", [f"{_OWN}/../{_TENANT_B}/vc-01", f"{_OWN}/%2e%2e/{_TENANT_B}/vc-01", f"{_OWN}\\vc-01"]
)
def test_target_secret_ref_that_could_move_is_refused_when_the_tenant_prefix_is_off(
    monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """With an empty prefix, the target write gate still refuses such a ref; a plain one passes."""
    from meho_backplane.api.v1.targets import _enforce_secret_ref_tenant_scope

    _set_env(monkeypatch, VAULT_KV_TENANT_SCOPE_PREFIX="")
    with pytest.raises(HTTPException) as exc:
        _enforce_secret_ref_tenant_scope(ref, _operator(), "vc-01")
    assert exc.value.status_code == 422
    detail: Any = exc.value.detail
    assert detail["kind"] == "secret_ref_outside_tenant_scope"
    assert detail["tenant_prefix"] == ""
    assert "a '.' or '..' segment, a '%' or a backslash" in detail["message"]
    _enforce_secret_ref_tenant_scope(f"{_OTHER}/vc-01", _operator(), "vc-01")
