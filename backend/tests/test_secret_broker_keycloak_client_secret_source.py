# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-kind secret move — keycloak client-secret SOURCE → vault sink (#3619).

Proves the broker's first Keycloak **source**: a single ``secret.move``
reads a confidential client's secret from a ``keycloak:`` client-secret
source and writes it into a ``vault:`` KV-v2 field, server-side, with the
value never crossing back to the caller.

Unlike the user-password sink (``test_secret_broker_keycloak_sink.py``),
a client secret IS served by the Admin REST API
(``GET /admin/realms/{realm}/clients/{uuid}/client-secret`` →
``{"type":"secret","value":…}``), so a broker source is legitimate. The
source reuses the connector's admin-read path (``_find_client_uuid`` +
``_get_admin_json``) exactly as the sink reuses the admin-write path — no
new HTTP client.

The direction is the mirror of the sink suite: the source is a
respx-mocked Keycloak Admin REST host plus a seeded ``KeycloakConnector``
with a stub admin-credential loader, and the sink is the shared in-process
Vault fake (``install_fake_client``) recording the write to ``put_calls``.
The load-bearing security assertion mirrors the sink suite: the sentinel
value appears in the Vault sink write and **nowhere else** — not in the
``secret.move`` op params, the op response JSON, the captured log records,
or the persisted audit row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.secret  # noqa: F401 -- registers the keycloak + vault kinds
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.secret_endpoint import (
    KeycloakClientNotFoundError,
    KeycloakClientSecretSourceEndpoint,
    KeycloakCredentialSecretEndpoint,
    KeycloakSecretRefError,
    build_keycloak_secret_endpoint,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

#: The client secret the Keycloak source serves. The whole point of this
#: suite is that it appears ONLY in the mocked Vault sink write.
_SENTINEL = "keycloak-sourced-client-secret-DO-NOT-LEAK-vault-sink"

_KC_HOST = "keycloak-secret-source.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_TOKEN = "kc-admin-token-secret-source"
_TARGET_NAME = "my-keycloak-secret-source"
_REALM = "example-realm"
_CLIENT_ID = "launcher-app"
_CLIENT_UUID = "55555555-5555-5555-5555-555555555555"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000af")
_OPERATOR = Operator(
    sub="secret-source-test",
    name=None,
    email=None,
    raw_jwt="<secret-source-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_EXISTING_CLIENT = {"id": _CLIENT_UUID, "clientId": _CLIENT_ID, "enabled": True}


# ---------------------------------------------------------------------------
# Fixtures — settings env, dispatcher isolation, seeded target + connector
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> Any:
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _seed_keycloak_target() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="keycloak",
            host=_KC_HOST,
            port=443,
            fqdn=None,
            secret_ref="tenant/keycloak/admin",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "26.0.5"},
            notes="seeded by test_secret_broker_keycloak_client_secret_source",
        )
        session.add(target)
        await session.commit()


@pytest.fixture
async def keycloak_source_env(
    stub_embedding_service: Any,
) -> AsyncIterator[KeycloakConnector]:
    """Seed the move op, the keycloak target, and a stubbed connector instance."""
    set_default_reducer(PassThroughReducer())
    await register_secret_broker_operations(embedding_service=stub_embedding_service)
    await _seed_keycloak_target()
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


def _mount_admin_token(mock: respx.MockRouter) -> None:
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
    )


async def _dispatch_move(params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_OPERATOR,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
        _approved=True,
    )


async def _fetch_move_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return [r for r in result.scalars().all() if r.path == "secret.move"]


# ---------------------------------------------------------------------------
# Dual dispatch — the ``clients/`` marker routes source vs sink
# ---------------------------------------------------------------------------


def test_build_routes_client_secret_ref_to_source() -> None:
    """A ``clients/…#secret`` ref builds the client-secret SOURCE endpoint."""
    ref = f"{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}#secret"
    assert isinstance(build_keycloak_secret_endpoint(ref), KeycloakClientSecretSourceEndpoint)


def test_build_routes_user_password_ref_to_sink() -> None:
    """A three-segment ref (incl. a user literally named ``clients``) stays a SINK."""
    user_endpoint = build_keycloak_secret_endpoint(f"{_TARGET_NAME}/{_REALM}/operator-a#password")
    assert isinstance(user_endpoint, KeycloakCredentialSecretEndpoint)
    # A user literally named "clients" is three segments → still the sink.
    clients_user = build_keycloak_secret_endpoint(f"{_TARGET_NAME}/{_REALM}/clients#password")
    assert isinstance(clients_user, KeycloakCredentialSecretEndpoint)


# ---------------------------------------------------------------------------
# Ref grammar — malformed / unsupported-field rejection (value-free)
# ---------------------------------------------------------------------------


def test_client_secret_ref_rejects_non_secret_field() -> None:
    """Any ``#field`` other than ``secret`` on a ``clients/`` ref is rejected."""
    with pytest.raises(KeycloakSecretRefError, match="unsupported field"):
        build_keycloak_secret_endpoint(f"{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}#password")


@pytest.mark.parametrize(
    "ref",
    [
        f"{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}",  # missing #field
        f"{_TARGET_NAME}/{_REALM}/notclients/{_CLIENT_ID}#secret",  # wrong marker segment
        f"{_TARGET_NAME}/{_REALM}/clients/#secret",  # empty clientId segment
        f"{_TARGET_NAME}/{_REALM}/clients#secret",  # too few segments
        f"{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}/extra#secret",  # too many segments
    ],
    ids=["no-field", "wrong-marker", "empty-clientid", "too-few", "too-many"],
)
def test_client_secret_source_rejects_malformed(ref: str) -> None:
    with pytest.raises(KeycloakSecretRefError):
        KeycloakClientSecretSourceEndpoint(ref)


# ---------------------------------------------------------------------------
# Source-only — a client-secret ref cannot be written to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_secret_source_write_is_unsupported() -> None:
    """``write_secret`` on a client-secret ref raises — it is a source, not a sink."""
    from meho_backplane.connectors.secret.endpoints import SecretMaterial

    endpoint = KeycloakClientSecretSourceEndpoint(
        f"{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}#secret"
    )
    with pytest.raises(NotImplementedError, match="source, not a sink"):
        await endpoint.write_secret(_OPERATOR, SecretMaterial("irrelevant"))


# ---------------------------------------------------------------------------
# clientId not found — the admin exact lookup returns no client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_secret_source_client_not_found(
    keycloak_source_env: KeycloakConnector,
) -> None:
    """A ``clientId`` with no matching client raises ``KeycloakClientNotFoundError``.

    The Admin REST ``?clientId=<id>`` exact lookup returns an empty list,
    so there is no ``uuid`` to read a secret for and the client-secret GET
    is never issued.
    """
    endpoint = KeycloakClientSecretSourceEndpoint(
        f"{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}#secret"
    )
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_token(mock)
        list_route = mock.get(f"/admin/realms/{_REALM}/clients").respond(200, json=[])
        secret_route = mock.get(
            f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/client-secret"
        ).respond(200, json={"type": "secret", "value": _SENTINEL})

        with pytest.raises(KeycloakClientNotFoundError, match="no client with clientId"):
            await endpoint.read_secret(_OPERATOR)

    assert list_route.called, "should have attempted the clientId → UUID lookup"
    assert not secret_route.called, "must not read a secret when the client does not exist"


# ---------------------------------------------------------------------------
# Cross-kind move — value reaches the vault sink, never the caller/audit/logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_source_to_vault_move_reads_secret_server_side(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    keycloak_source_env: KeycloakConnector,
) -> None:
    """A keycloak: → vault: move reads the client secret; value stays server-side.

    Asserts the sentinel is written to the Vault sink field and appears
    NOWHERE else — not in the op response, the op params, the captured
    logs, or the persisted audit row.
    """
    fake = install_fake_client(monkeypatch)

    params = {
        "from": f"keycloak:{_TARGET_NAME}/{_REALM}/clients/{_CLIENT_ID}#secret",
        # Sink lives in the operator's own tenant subtree so it passes the
        # default-on vault-kv tenant-scope guard on the write path (S08 #296).
        "to": f"vault:tenants/{_OPERATOR_TENANT_ID}/{_CLIENT_ID}#client_secret",
        "reason": "provision launcher client secret into vault",
    }

    with (
        caplog.at_level(logging.DEBUG),
        respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock,
    ):
        _mount_admin_token(mock)
        list_route = mock.get(f"/admin/realms/{_REALM}/clients").respond(
            200, json=[_EXISTING_CLIENT]
        )
        secret_route = mock.get(
            f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/client-secret"
        ).respond(200, json={"type": "secret", "value": _SENTINEL})
        result = await _dispatch_move(params)

    assert result.status == "ok", result.error

    # (a) The source resolved clientId→UUID and read the client-secret.
    assert list_route.called, "should have looked up the clientId → UUID"
    assert secret_route.called, "should have read the client-secret endpoint"

    # (b) The sentinel value landed in the Vault sink field...
    assert fake.secrets.kv.v2.put_calls == [
        {
            "path": f"tenants/{_OPERATOR_TENANT_ID}/{_CLIENT_ID}",
            "secret": {"client_secret": _SENTINEL},
            "cas": None,
            "mount_point": "secret",
        }
    ]

    # (c) ...and NOWHERE else: response, params, logs.
    assert result.result == {
        "status": "moved",
        "value_sha256": hashlib.sha256(_SENTINEL.encode()).hexdigest(),
        "length": len(_SENTINEL.encode()),
    }
    assert _SENTINEL not in result.model_dump_json()
    assert _SENTINEL not in json.dumps(params)
    assert _SENTINEL not in caplog.text

    # (d) ...and not in the persisted audit row (payload + raw_payload).
    move_rows = await _fetch_move_audit_rows()
    assert len(move_rows) == 1
    row = move_rows[0]
    assert _SENTINEL not in json.dumps(row.payload)
    assert _SENTINEL not in json.dumps(row.raw_payload)
