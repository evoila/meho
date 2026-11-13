# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Keycloak target names its managed realm; MEHO has no built-in one.

* A target with no managed realm is refused before any call to Keycloak
  (operations, fingerprint, probe).
* Creating or updating a Keycloak target without one is refused with a 422.
* A target with a realm works as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    DEFAULT_ADMIN_REALM,
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakManagedRealmMissingError,
    KeycloakTargetLike,
    resolve_realm_config,
)
from meho_backplane.targets.required_extras import (
    check_keycloak_managed_realm,
    managed_realm_missing_message,
)

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_BASE_URL = "https://keycloak-a.test.invalid"


@dataclass
class _StubTarget:
    name: str = "keycloak-a"
    host: str = "keycloak-a.test.invalid"
    port: int | None = 443
    secret_ref: str | None = "tenant/keycloak/admin"
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=dict)
    tls_server_name: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="operator.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _connector() -> tuple[KeycloakConnector, list[str]]:
    """A connector whose credential loader records each call."""
    loads: list[str] = []

    async def _load(_target: KeycloakTargetLike, _operator: Operator) -> KeycloakAdminCredentials:
        loads.append("loaded")
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="s3cret")

    return KeycloakConnector(credentials_loader=_load), loads


# ---------------------------------------------------------------------------
# resolve_realm_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extras",
    [
        {},
        {"managed_realm": ""},
        {"managed_realm": "   "},
        {"managed_realm": None},
        {"managed_realm": 7},
        {"admin_realm": "x"},
    ],
)
def test_resolve_realm_config_refuses_a_target_without_a_realm(extras: dict[str, Any]) -> None:
    with pytest.raises(KeycloakManagedRealmMissingError) as excinfo:
        resolve_realm_config(_StubTarget(extras=extras))
    text = str(excinfo.value)
    assert "keycloak-a" in text
    assert "--extra managed_realm=<realm>" in text


def test_resolve_realm_config_refuses_a_target_without_extras_attribute() -> None:
    class _Bare:
        name = "bare"

    with pytest.raises(KeycloakManagedRealmMissingError):
        resolve_realm_config(_Bare())  # type: ignore[arg-type]


def test_resolve_realm_config_uses_the_target_realm_and_keeps_the_admin_default() -> None:
    realms = resolve_realm_config(_StubTarget(extras={"managed_realm": "example"}))
    assert realms.managed_realm == "example"
    assert realms.admin_realm == DEFAULT_ADMIN_REALM == "master"


def test_resolve_realm_config_reads_a_named_admin_realm() -> None:
    realms = resolve_realm_config(
        _StubTarget(extras={"managed_realm": "example", "admin_realm": "ops"})
    )
    assert (realms.admin_realm, realms.managed_realm) == ("ops", "example")


# ---------------------------------------------------------------------------
# The connector refuses before any Keycloak call
# ---------------------------------------------------------------------------


_NO_REALM_EXTRAS = [{}, {"managed_realm": "   "}]


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", _NO_REALM_EXTRAS)
async def test_operation_without_a_realm_is_refused_before_any_call(
    extras: dict[str, Any],
) -> None:
    connector, loads = _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        with pytest.raises(KeycloakManagedRealmMissingError):
            await connector.realm_get(_operator(), _StubTarget(extras=extras), {})
        assert mock.calls.call_count == 0
    assert loads == []


@pytest.mark.asyncio
async def test_operation_with_a_realm_runs_as_before() -> None:
    connector, _loads = _connector()
    target = _StubTarget(extras={"managed_realm": "example"})
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-token", "expires_in": 60}
        )
        mock.get("/admin/realms/example").respond(200, json={"realm": "example"})
        result = await connector.realm_get(_operator(), target, {})
    assert result["realm"]["realm"] == "example"


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", _NO_REALM_EXTRAS)
async def test_fingerprint_and_probe_report_a_target_without_a_realm(
    extras: dict[str, Any],
) -> None:
    connector, loads = _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        fp = await connector.fingerprint(_StubTarget(extras=extras), operator=_operator())
        probe = await connector.probe(_StubTarget(extras=extras))
        assert mock.calls.call_count == 0
    assert fp.reachable is False
    assert "--extra managed_realm=<realm>" in fp.extras["error"]
    assert probe.ok is False
    assert probe.reason is not None
    assert "names no managed realm" in probe.reason
    assert loads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", _NO_REALM_EXTRAS)
async def test_check_login_with_an_explicit_realm_still_needs_the_target_realm(
    extras: dict[str, Any],
) -> None:
    from meho_backplane.connectors.keycloak.ops_login_check import keycloak_user_check_login

    connector, loads = _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        with pytest.raises(KeycloakManagedRealmMissingError):
            await keycloak_user_check_login(
                connector,
                _operator(),
                _StubTarget(extras=extras),
                {"username": "operator-a", "realm": "example", "password_ref": "x"},
            )
        assert mock.calls.call_count == 0
    assert loads == []


# ---------------------------------------------------------------------------
# The shared check
# ---------------------------------------------------------------------------


def test_check_refuses_only_a_keycloak_target_without_a_realm() -> None:
    with pytest.raises(ValueError, match="names no managed realm"):
        check_keycloak_managed_realm("keycloak", {}, "kc")
    with pytest.raises(ValueError, match="names no managed realm"):
        check_keycloak_managed_realm("Keycloak", {"managed_realm": "  "}, "kc")
    check_keycloak_managed_realm("keycloak", {"managed_realm": "example"}, "kc")
    check_keycloak_managed_realm("ssh", {}, "box")


def test_message_names_the_target_and_the_command() -> None:
    text = managed_realm_missing_message("kc-1")
    assert "'kc-1'" in text
    assert "meho targets update" in text
    assert "meho targets add" in text


# ---------------------------------------------------------------------------
# Create and update (REST)
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _auth(key: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {_admin_token(key)}"}


def test_create_keycloak_target_without_a_realm_is_refused(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "kc-new", "product": "keycloak", "host": "keycloak-a.test.invalid"},
            headers=_auth(key),
        )
        listed = client.get("/api/v1/targets", headers=_auth(key)).json()["items"]
    assert response.status_code == 422
    assert "names no managed realm" in response.json()["detail"]
    assert listed == []


def test_create_keycloak_target_with_a_realm_works(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "kc-new",
                "product": "keycloak",
                "host": "keycloak-a.test.invalid",
                "extras": {"managed_realm": "example"},
            },
            headers=_auth(key),
        )
    assert response.status_code == 201
    assert response.json()["extras"] == {"managed_realm": "example"}


def test_update_keycloak_target_without_a_realm_is_refused(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        created = client.post(
            "/api/v1/targets",
            json={
                "name": "kc-upd",
                "product": "keycloak",
                "host": "keycloak-a.test.invalid",
                "extras": {"managed_realm": "example"},
            },
            headers=_auth(key),
        )
        assert created.status_code == 201
        cleared = client.patch(
            "/api/v1/targets/kc-upd", json={"extras": {"admin_realm": "master"}}, headers=_auth(key)
        )
        moved = client.patch(
            "/api/v1/targets/kc-upd",
            json={"extras": {"managed_realm": "other"}},
            headers=_auth(key),
        )
        unrelated = client.patch("/api/v1/targets/kc-upd", json={"notes": "n"}, headers=_auth(key))
    assert cleared.status_code == 422
    assert "names no managed realm" in cleared.json()["detail"]
    assert moved.status_code == 200
    assert moved.json()["extras"] == {"managed_realm": "other"}
    assert unrelated.status_code == 200


def test_update_to_keycloak_product_needs_a_realm(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        created = client.post(
            "/api/v1/targets",
            json={"name": "box", "product": "ssh", "host": "box.test.invalid"},
            headers=_auth(key),
        )
        assert created.status_code == 201
        refused = client.patch(
            "/api/v1/targets/box", json={"product": "keycloak"}, headers=_auth(key)
        )
        accepted = client.patch(
            "/api/v1/targets/box",
            json={"product": "keycloak", "extras": {"managed_realm": "example"}},
            headers=_auth(key),
        )
    assert refused.status_code == 422
    assert accepted.status_code == 200
