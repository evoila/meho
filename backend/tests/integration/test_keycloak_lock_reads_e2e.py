# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The two Keycloak lock reads, against a real Keycloak.

``keycloak.user.lock_status`` and ``keycloak.realm.lock_settings`` are run on
a real Keycloak container (the ``keycloak_bootstrap`` fixture), with the
connector's own admin sign-in. The recorded-answer unit tests cannot prove
that Keycloak really answers in the shapes the reads expect. This test does:

1. After two failed sign-ins the failure count is 2 and the user is not
   locked. After the third (the realm allows 3) the user is locked.
2. The same read for an id that does not exist is "user not found".
3. The realm's lock rules read back as set, with the lock on and off.

The test skips when Docker or ``MEHO_TEST_KEYCLOAK_IMAGE`` is missing, like
every other test that uses the Keycloak container.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.session import KeycloakPasswordCredentials
from tests.integration.conftest import (
    _KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD,
    _KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME,
    KeycloakBootstrap,
)

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000006453")
_FAILURE_FACTOR = 3
_CLIENT_ID = "lock-reads-test"


@dataclass(frozen=True)
class _Target:
    """The minimum target shape the connector reads."""

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None = None
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=dict)


def _operator() -> Operator:
    return Operator(
        sub="lock-reads-test",
        name="Lock Reads Test",
        email=None,
        raw_jwt="<lock-reads-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _admin_headers(kc: KeycloakBootstrap) -> dict[str, str]:
    resp = httpx.post(
        f"{kc.base_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME,
            "password": _KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD,
        },
    )
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _set_realm_lock(kc: KeycloakBootstrap, *, on: bool) -> None:
    """Switch the realm's lock on or off. When on, it locks after 3 failures."""
    realm = httpx.get(kc.admin_url, headers=_admin_headers(kc)).json()
    realm.update(
        bruteForceProtected=on,
        permanentLockout=False,
        failureFactor=_FAILURE_FACTOR,
        waitIncrementSeconds=60,
        maxFailureWaitSeconds=900,
        maxDeltaTimeSeconds=43200,
        # A second sign-in failure within this time locks at once; keep it clear of the test.
        quickLoginCheckMilliSeconds=100,
    )
    httpx.put(kc.admin_url, headers=_admin_headers(kc), json=realm).raise_for_status()


def _failed_sign_in(kc: KeycloakBootstrap, username: str) -> None:
    resp = httpx.post(
        f"{kc.issuer_url}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": _CLIENT_ID,
            "username": username,
            "password": "not-the-password",
        },
    )
    assert resp.status_code in (400, 401), resp.text
    time.sleep(0.3)


@pytest.fixture(scope="module")
def lock_user(keycloak_bootstrap: KeycloakBootstrap) -> Iterator[tuple[str, str]]:
    """A user and a sign-in client in the realm, with the lock on. Returns (id, username)."""
    kc = keycloak_bootstrap
    headers = _admin_headers(kc)
    httpx.post(
        f"{kc.admin_url}/clients",
        headers=headers,
        json={"clientId": _CLIENT_ID, "publicClient": True, "directAccessGrantsEnabled": True},
    ).raise_for_status()
    username = "lock-reads-user"
    httpx.post(
        f"{kc.admin_url}/users",
        headers=headers,
        json={
            "username": username,
            "enabled": True,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "firstName": "Lock",
            "lastName": "Reads",
            "credentials": [
                {"type": "password", "value": "a-made-up-pass-6453", "temporary": False}
            ],
        },
    ).raise_for_status()
    rows = httpx.get(
        f"{kc.admin_url}/users", headers=headers, params={"username": username, "exact": "true"}
    ).json()
    _set_realm_lock(kc, on=True)
    yield str(rows[0]["id"]), username


@pytest.fixture
async def connector(keycloak_bootstrap: KeycloakBootstrap) -> AsyncIterator[KeycloakConnector]:
    async def load(_target: Any, _operator: Operator) -> KeycloakPasswordCredentials:
        return KeycloakPasswordCredentials(
            username=_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME, password=_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD
        )

    conn = KeycloakConnector(credentials_loader=load)
    yield conn
    await conn.aclose()


def _target(kc: KeycloakBootstrap) -> _Target:
    host, port = kc.base_url.removeprefix("http://").split(":")
    return _Target(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name="lock-reads-e2e",
        host=host,
        port=int(port),
        extras={"scheme": "http", "admin_realm": "master", "managed_realm": kc.realm},
    )


async def test_failed_sign_ins_are_counted_and_then_lock_the_user(
    keycloak_bootstrap: KeycloakBootstrap,
    connector: KeycloakConnector,
    lock_user: tuple[str, str],
) -> None:
    user_id, username = lock_user
    _set_realm_lock(keycloak_bootstrap, on=True)
    target = _target(keycloak_bootstrap)

    clean = await connector.user_lock_status(_operator(), target, {"id": user_id})
    assert clean["locked"] is False
    assert not clean["numFailures"]

    for _ in range(_FAILURE_FACTOR - 1):
        _failed_sign_in(keycloak_bootstrap, username)
    some = await connector.user_lock_status(_operator(), target, {"username": username})
    assert some["userId"] == user_id
    assert some["numFailures"] == _FAILURE_FACTOR - 1
    assert some["locked"] is False
    assert isinstance(some["lastFailure"], str)
    assert "lastIPFailure" not in some

    _failed_sign_in(keycloak_bootstrap, username)
    locked = await connector.user_lock_status(_operator(), target, {"id": user_id})
    assert locked["numFailures"] == _FAILURE_FACTOR
    assert locked["locked"] is True


async def test_unknown_user_id_is_user_not_found(
    keycloak_bootstrap: KeycloakBootstrap,
    connector: KeycloakConnector,
    lock_user: tuple[str, str],
) -> None:
    _set_realm_lock(keycloak_bootstrap, on=True)
    with pytest.raises(KeycloakUserNotFoundError):
        await connector.user_lock_status(
            _operator(), _target(keycloak_bootstrap), {"id": str(uuid.uuid4())}
        )


async def test_realm_lock_settings_read_back_with_the_lock_on_and_off(
    keycloak_bootstrap: KeycloakBootstrap,
    connector: KeycloakConnector,
    lock_user: tuple[str, str],
) -> None:
    target = _target(keycloak_bootstrap)
    _set_realm_lock(keycloak_bootstrap, on=True)
    on = await connector.realm_lock_settings(_operator(), target, {})
    assert on["realm"] == keycloak_bootstrap.realm
    assert on["bruteForceProtected"] is True
    assert on["failureFactor"] == _FAILURE_FACTOR
    assert on["permanentLockout"] is False

    _set_realm_lock(keycloak_bootstrap, on=False)
    off = await connector.realm_lock_settings(_operator(), target, {})
    assert off["bruteForceProtected"] is False
    _set_realm_lock(keycloak_bootstrap, on=True)
