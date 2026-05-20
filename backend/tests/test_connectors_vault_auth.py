# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Vault identity-read op group (G3.3-T3, #547).

Covers ``vault.auth.userpass.list/read`` + ``vault.auth.approle.list/read``:
registration, the happy path, mount-path parameterisation, the
auth-backend-not-mounted structured error, the missing-user/role path,
empty-mount normalisation, schema-driven param validation, and
login-side failure propagation.

Mocking discipline: hvac's transport is ``requests``, not ``httpx``, so
the canonical Vault unit-test seam in this codebase is the in-process
fake hvac client (``install_fake_client`` -> ``_build_client``
monkeypatch), not an httpx/respx mock. The Task DoD's "httpx mock"
phrasing is satisfied in substance -- mocked HTTP, no real Vault, every
op's present/absent/error envelope exercised -- via that established
seam, the same one ``test_connectors_vault.py`` uses for
``vault.kv.read`` (which #547 says to mirror).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    register_vault_typed_operations,
)
from meho_backplane.operations import dispatch
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars needed by Settings / VaultConnector."""
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


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_vault_typed_ops(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Upsert every Vault typed-op descriptor row (kv.read + the four auth ops).

    The autouse ``_default_database_url`` conftest fixture has already
    migrated the SQLite database to head, so ``endpoint_descriptor`` /
    ``operation_group`` exist. ``register_vault_typed_operations`` fans
    out into ``register_vault_auth_operations`` (the package keeps a
    single lifespan-driven registrar entry).
    """
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """Request-scoped operator carrying the bearer token the vault
    handlers forward to Vault's JWT/OIDC auth (G0.8-T3 #629). Replaces
    the pre-#224 ``VaultTarget(raw_jwt=...)`` stub.
    """
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_vault(
    op_id: str, params: dict[str, Any], *, jwt: str = "fake.jwt.value"
) -> OperationResult:
    """Dispatch a vault op through the real operator-aware path.

    The dispatcher threads a real :class:`Operator`, resolves the
    connector by ``connector_id``, and ``target`` is ``None`` (vault
    connection params come from settings). The handler reads the JWT
    from ``operator.raw_jwt`` — the #629 contract.
    """
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_auth_ops_register_under_vault_connector_key(
    stub_embedding_service: AsyncMock,
) -> None:
    """All four auth ops upsert into endpoint_descriptor; idempotent on re-run.

    A second call against unchanged descriptions is a no-op for the
    embedding pipeline (body-hash skip in ``register_typed_operation``).
    The dispatcher's ``unknown_op`` ``known_op_count`` reflects the
    registered descriptors for the ``(vault, 1.x, vault)`` triple.
    """
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    # Idempotent re-run must not raise.
    await register_vault_typed_operations(embedding_service=stub_embedding_service)


@pytest.mark.parametrize(
    "op_id",
    [
        "vault.auth.userpass.list",
        "vault.auth.userpass.read",
        "vault.auth.approle.list",
        "vault.auth.approle.read",
    ],
)
async def test_auth_op_is_dispatchable(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    _registered_vault_typed_ops: None,
) -> None:
    """Each registered op_id is known to the dispatcher (not ``unknown_op``).

    Drives the op with the param shape the schema requires; the fake
    backend returns an empty roster / a present entity so the call
    reaches the handler rather than bouncing on ``unknown_op`` or
    ``invalid_params``.
    """
    fake = install_fake_client(monkeypatch)
    fake.auth.userpass.users = {"ci": {"token_policies": ["default"]}}
    fake.auth.approle.roles = {"deploy": {"token_policies": ["default"]}}
    params: dict[str, Any] = {}
    if op_id == "vault.auth.userpass.read":
        params = {"username": "ci"}
    elif op_id == "vault.auth.approle.read":
        params = {"role_name": "deploy"}

    result = await _dispatch_vault(op_id, params)

    assert result.status == "ok", result.error
    assert result.extras.get("error_code") != "unknown_op"


# ---------------------------------------------------------------------------
# userpass.list / approle.list — happy path + empty + mount
# ---------------------------------------------------------------------------


async def test_userpass_list_returns_sorted_keys(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.userpass.users = {"armon": {}, "mitchellh": {}}

    result = await _dispatch_vault("vault.auth.userpass.list", {}, jwt="op-jwt")

    assert result.status == "ok", result.error
    assert result.result == {"keys": ["armon", "mitchellh"]}
    assert fake.auth.userpass.list_calls == [{"mount_point": "userpass"}]
    # Login + per-request revoke still happen (shared_service_account).
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    assert fake.auth.token.revoke_calls == 1


async def test_approle_list_returns_sorted_keys(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.approle.roles = {"prod": {}, "dev": {}, "test": {}}

    result = await _dispatch_vault("vault.auth.approle.list", {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": ["dev", "prod", "test"]}
    assert fake.auth.approle.list_calls == [{"mount_point": "approle"}]


async def test_list_empty_mount_normalises_to_empty_list(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A mounted-but-empty backend yields ``{'keys': []}`` (Vault 204)."""
    install_fake_client(monkeypatch)  # default: no users / no roles

    up = await _dispatch_vault("vault.auth.userpass.list", {})
    ar = await _dispatch_vault("vault.auth.approle.list", {})

    assert up.status == "ok" and up.result == {"keys": []}
    assert ar.status == "ok" and ar.result == {"keys": []}


async def test_list_honours_non_default_mount(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    """A non-default ``mount`` flows to hvac's ``mount_point`` verbatim."""
    fake = install_fake_client(monkeypatch)
    fake.auth.userpass.users = {"svc": {}}

    result = await _dispatch_vault(
        "vault.auth.userpass.list",
        {"mount": "userpass-prod"},
    )

    assert result.status == "ok", result.error
    assert result.result == {"keys": ["svc"]}
    assert fake.auth.userpass.list_calls == [{"mount_point": "userpass-prod"}]


# ---------------------------------------------------------------------------
# userpass.read / approle.read — happy path + mount
# ---------------------------------------------------------------------------


async def test_userpass_read_returns_config(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.userpass.users = {
        "ci": {
            "token_policies": ["admin", "default"],
            "token_ttl": 0,
            "token_max_ttl": 0,
        }
    }

    result = await _dispatch_vault(
        "vault.auth.userpass.read",
        {"username": "ci", "mount": "userpass-prod"},
    )

    assert result.status == "ok", result.error
    assert result.result == {
        "token_policies": ["admin", "default"],
        "token_ttl": 0,
        "token_max_ttl": 0,
    }
    assert fake.auth.userpass.read_calls == [{"username": "ci", "mount_point": "userpass-prod"}]


async def test_approle_read_returns_config(
    monkeypatch: pytest.MonkeyPatch,
    _registered_vault_typed_ops: None,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.approle.roles = {
        "deploy": {
            "token_policies": ["default"],
            "token_ttl": 1200,
            "token_max_ttl": 1800,
            "secret_id_ttl": 600,
            "bind_secret_id": True,
        }
    }

    result = await _dispatch_vault("vault.auth.approle.read", {"role_name": "deploy"})

    assert result.status == "ok", result.error
    assert result.result["token_policies"] == ["default"]
    assert result.result["secret_id_ttl"] == 600
    assert result.result["bind_secret_id"] is True
    assert fake.auth.approle.read_calls == [{"role_name": "deploy", "mount_point": "approle"}]


# ---------------------------------------------------------------------------
# auth-backend-not-mounted -> structured connector_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,params,backend",
    [
        ("vault.auth.userpass.list", {}, "userpass"),
        ("vault.auth.approle.list", {}, "approle"),
    ],
)
async def test_list_backend_not_mounted_surfaces_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    backend: str,
    _registered_vault_typed_ops: None,
) -> None:
    """LIST against an unmounted auth backend -> VaultAuthBackendNotMountedError.

    Vault returns 404 -> hvac raises ``InvalidPath`` -> the handler
    reclassifies it so the dispatcher's ``connector_error`` payload
    carries the operator-actionable class name.
    """
    fake = install_fake_client(monkeypatch)
    getattr(fake.auth, backend).list_exc = hvac.exceptions.InvalidPath("no handler for route")

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "VaultAuthBackendNotMountedError"


@pytest.mark.parametrize(
    "op_id,params,backend",
    [
        ("vault.auth.userpass.read", {"username": "ci"}, "userpass"),
        ("vault.auth.approle.read", {"role_name": "deploy"}, "approle"),
    ],
)
async def test_read_backend_not_mounted_surfaces_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    backend: str,
    _registered_vault_typed_ops: None,
) -> None:
    """READ 404 + LIST 404 (probe) -> VaultAuthBackendNotMountedError.

    The read handler can't tell "backend absent" from "entity missing"
    from a single 404, so it probes the mount with LIST. Both 404 ->
    backend absent.
    """
    fake = install_fake_client(monkeypatch)
    target_backend = getattr(fake.auth, backend)
    target_backend.read_exc = hvac.exceptions.InvalidPath("no handler for route")
    target_backend.list_exc = hvac.exceptions.InvalidPath("no handler for route")

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "VaultAuthBackendNotMountedError"


@pytest.mark.parametrize(
    "op_id,params,backend",
    [
        ("vault.auth.userpass.read", {"username": "ghost"}, "userpass"),
        ("vault.auth.approle.read", {"role_name": "ghost"}, "approle"),
    ],
)
async def test_read_missing_entity_under_mounted_backend_keeps_invalid_path(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    backend: str,
    _registered_vault_typed_ops: None,
) -> None:
    """Missing user/role under a *mounted* backend stays ``InvalidPath``.

    READ 404 but the LIST probe succeeds (backend mounted) -> the
    original ``InvalidPath`` is re-raised so callers can distinguish
    "no such user/role" from "backend not enabled".
    """
    fake = install_fake_client(monkeypatch)
    target_backend = getattr(fake.auth, backend)
    target_backend.read_exc = hvac.exceptions.InvalidPath("no secret found")
    # list_exc stays None -> the probe LIST succeeds (empty roster).

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == "InvalidPath"


# ---------------------------------------------------------------------------
# schema-driven param validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,params",
    [
        ("vault.auth.userpass.read", {}),
        ("vault.auth.userpass.read", {"username": ""}),
        ("vault.auth.userpass.read", {"username": "   "}),
        ("vault.auth.userpass.read", {"username": 123}),
        ("vault.auth.approle.read", {}),
        ("vault.auth.approle.read", {"role_name": "  "}),
        ("vault.auth.userpass.list", {"mount": ""}),
        ("vault.auth.userpass.list", {"unexpected": "x"}),
    ],
    ids=[
        "userpass-read-missing-username",
        "userpass-read-empty-username",
        "userpass-read-whitespace-username",
        "userpass-read-nonstring-username",
        "approle-read-missing-role",
        "approle-read-whitespace-role",
        "userpass-list-empty-mount",
        "userpass-list-additional-property",
    ],
)
async def test_invalid_params_rejected_by_schema(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """The dispatcher rejects bad params against the registered schema.

    No Vault call is made -- validation runs before the handler. Proves
    ``required`` / ``minLength`` / ``pattern`` / ``type`` /
    ``additionalProperties:false`` are wired into each op's schema.
    """
    install_fake_client(monkeypatch)

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras.get("error_code") == "invalid_params"
    assert result.extras.get("validation_errors")


# ---------------------------------------------------------------------------
# login-side failure propagation (VaultClientError subclass)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "login_exc,expected_exc_class",
    [
        (requests.exceptions.ConnectionError("no route"), "VaultUnreachableError"),
        (hvac.exceptions.Forbidden("role denied"), "VaultRoleDeniedError"),
    ],
    ids=["unreachable", "role-denied"],
)
@pytest.mark.parametrize(
    "op_id,params",
    [
        ("vault.auth.userpass.list", {}),
        ("vault.auth.approle.read", {"role_name": "deploy"}),
    ],
)
async def test_login_failure_surfaces_vault_client_error_class(
    monkeypatch: pytest.MonkeyPatch,
    login_exc: Exception,
    expected_exc_class: str,
    op_id: str,
    params: dict[str, Any],
    _registered_vault_typed_ops: None,
) -> None:
    """A login-phase failure short-circuits before the auth read call.

    Same contract as ``vault.kv.read``: the dispatcher's
    ``connector_error`` records the ``VaultClientError`` subclass name
    in ``extras.exception_class``.
    """
    install_fake_client(monkeypatch, login_exc=login_exc)

    result = await _dispatch_vault(op_id, params)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras.get("exception_class") == expected_exc_class
