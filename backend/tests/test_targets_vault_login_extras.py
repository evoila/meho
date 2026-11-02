# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Only a platform admin may set or change a target's Vault login role.

Covers :mod:`meho_backplane.targets.vault_login_extras` on its own and
through ``POST`` / ``PATCH /api/v1/targets``. A target that already
carries ``vault_role`` keeps it through an unrelated ``PATCH`` and still
logs in under it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vault.target_auth import (
    VaultTargetAuth,
    resolve_vault_target_auth,
    vault_client_for_target,
)
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target
from meho_backplane.targets.vault_login_extras import (
    VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN,
    changed_vault_login_keys,
    enforce_vault_login_extras_platform_admin,
)

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_ROLE_EXTRAS = {"vault_role": "narrow-role", "vault_mount": "jwt-other"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _platform_admin_token(key: Any) -> str:
    return mint_token(
        key,
        sub="padmin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=DEFAULT_TENANT_ID,
        platform_admin=True,
    )


def _operator(*, platform_admin: bool) -> Operator:
    return Operator(
        sub="op-1",
        name=None,
        email=None,
        raw_jwt="op-jwt",
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
    )


# ---------------------------------------------------------------------------
# The gate on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (None, None, []),
        ({}, {"other": 1}, []),
        (None, {"vault_role": "r"}, ["vault_role"]),
        ({"vault_role": "r"}, {"vault_role": "r", "other": 1}, []),
        ({"vault_role": "r"}, {"vault_role": "s"}, ["vault_role"]),
        ({"vault_role": "r"}, {}, ["vault_role"]),
        ({"vault_role": "r"}, None, ["vault_role"]),
        ({"vault_role": "r"}, {"vault_role": ""}, ["vault_role"]),
        ({}, {"vault_mount": "m"}, ["vault_mount"]),
        ({}, {"vault_role": "r", "vault_mount": "m"}, ["vault_role", "vault_mount"]),
        ({}, {"vault_role": None}, []),
    ],
    ids=[
        "both-none",
        "other-key-only",
        "add-role",
        "same-role-plus-other-key",
        "change-role",
        "remove-role",
        "clear-extras",
        "blank-role-is-a-change",
        "add-mount",
        "add-both",
        "null-role-equals-absent",
    ],
)
def test_changed_vault_login_keys(
    before: dict[str, Any] | None, after: dict[str, Any] | None, expected: list[str]
) -> None:
    assert changed_vault_login_keys(before, after) == expected


def test_gate_refuses_change_for_non_platform_admin() -> None:
    with pytest.raises(HTTPException) as exc:
        enforce_vault_login_extras_platform_admin(
            _operator(platform_admin=False), before=None, after=_ROLE_EXTRAS, target_name="t"
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN


def test_gate_passes_platform_admin_and_unchanged_keys() -> None:
    enforce_vault_login_extras_platform_admin(
        _operator(platform_admin=True), before=None, after=_ROLE_EXTRAS, target_name="t"
    )
    enforce_vault_login_extras_platform_admin(
        _operator(platform_admin=False),
        before=_ROLE_EXTRAS,
        after={**_ROLE_EXTRAS, "other": "x"},
        target_name="t",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/targets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extras",
    [{"vault_role": "narrow-role"}, {"vault_mount": "jwt-other"}, _ROLE_EXTRAS],
    ids=["role", "mount", "both"],
)
def test_create_with_vault_login_keys_needs_platform_admin(
    client: TestClient, extras: dict[str, Any]
) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "vault-a", "product": "vault", "host": "10.0.0.9", "extras": extras},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN

        listed = client.get(
            "/api/v1/targets", headers={"Authorization": f"Bearer {_admin_token(key)}"}
        )
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_create_with_other_extras_stays_open_to_tenant_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "vault-b",
                "product": "vault",
                "host": "10.0.0.9",
                "extras": {"sso_realm": "lab"},
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201, response.text
    assert response.json()["extras"] == {"sso_realm": "lab"}


def test_create_with_vault_login_keys_as_platform_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "vault-c",
                "product": "vault",
                "host": "10.0.0.9",
                "extras": _ROLE_EXTRAS,
            },
            headers={"Authorization": f"Bearer {_platform_admin_token(key)}"},
        )
    assert response.status_code == 201, response.text
    assert response.json()["extras"] == _ROLE_EXTRAS
    assert resolve_vault_target_auth(Target(**response.json())) == VaultTargetAuth(
        "narrow-role", "jwt-other"
    )


# ---------------------------------------------------------------------------
# PATCH /api/v1/targets/{name}
# ---------------------------------------------------------------------------


async def _existing_role_target() -> Any:
    return await _insert_target(
        name="vault-existing", product="vault", host="10.0.0.9", extras=dict(_ROLE_EXTRAS)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extras",
    [
        {"vault_role": "other-role", "vault_mount": "jwt-other"},
        {"vault_role": "narrow-role", "vault_mount": "jwt-third"},
        {"vault_mount": "jwt-other"},
        {},
        None,
    ],
    ids=["change-role", "change-mount", "remove-role", "remove-both", "clear-extras"],
)
async def test_update_vault_login_keys_needs_platform_admin(
    client: TestClient, extras: dict[str, Any] | None
) -> None:
    await _existing_role_target()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/vault-existing", json={"extras": extras}, headers=headers
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN
        after = client.get("/api/v1/targets/vault-existing", headers=headers)
    assert after.json()["extras"] == _ROLE_EXTRAS


@pytest.mark.asyncio
async def test_update_keeps_role_when_tenant_admin_resends_it(client: TestClient) -> None:
    await _existing_role_target()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/vault-existing",
            json={"extras": {**_ROLE_EXTRAS, "sso_realm": "lab"}},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["extras"] == {**_ROLE_EXTRAS, "sso_realm": "lab"}


@pytest.mark.asyncio
async def test_update_other_field_keeps_role_and_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing target keeps its role, and the login still uses it."""
    await _existing_role_target()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/vault-existing",
            json={"notes": "still mine"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["extras"] == _ROLE_EXTRAS

    get_settings.cache_clear()
    fake = install_fake_vault(monkeypatch)
    async with vault_client_for_target(_operator(platform_admin=False), Target(**response.json())):
        pass
    assert fake.auth.jwt.login_calls == [
        {"role": "narrow-role", "jwt": "op-jwt", "path": "jwt-other"},
    ]


@pytest.mark.asyncio
async def test_update_vault_login_keys_as_platform_admin(client: TestClient) -> None:
    await _existing_role_target()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/vault-existing",
            json={"extras": {"vault_role": "other-role"}},
            headers={"Authorization": f"Bearer {_platform_admin_token(key)}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["extras"] == {"vault_role": "other-role"}
