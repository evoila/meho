# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-kind secret move — vault source → keycloak sink (G0.22-T2 #1578).

Proves the broker's "≥2 connector kinds" definition of done: a single
``secret.move`` reads a credential from a ``vault:`` source and writes it
into a ``keycloak:`` user credential, server-side, with the value never
crossing back to the caller.

The source uses the shared in-process Vault fake (``install_fake_client``
— same seam ``test_connectors_secret_broker.py`` drives the vault→vault
move through). The sink uses a respx-mocked Keycloak Admin REST host plus
a seeded ``KeycloakConnector`` instance with a stub admin-credential
loader (the pattern from ``test_connectors_keycloak_write_e2e.py``), so
no live Keycloak / Vault is needed.

The load-bearing security assertion mirrors the keycloak write E2E
(``test_connectors_keycloak_write_e2e.py`` ``_PASSWORD_SENTINEL`` /
asserts at :452-453): the sentinel value appears in the mocked Admin REST
``reset-password`` PUT body and **nowhere else** — not in the
``secret.move`` op params, the op response JSON, the captured log
records, or the persisted audit row.
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
    KeycloakCredentialSecretEndpoint,
    KeycloakSecretRefError,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import SECRET_ENDPOINT_REGISTRY
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

#: The secret the Vault source serves. The whole point of this suite is
#: that it appears ONLY in the mocked Keycloak Admin PUT body.
_SENTINEL = "vault-sourced-password-DO-NOT-LEAK-keycloak-sink"

_KC_HOST = "keycloak-secret-sink.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_TOKEN = "kc-admin-token-secret-sink"
_TARGET_NAME = "rdc-keycloak-secret-sink"
_REALM = "evba"
_USERNAME = "operator-a"
_USER_UUID = "44444444-4444-4444-4444-444444444444"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ae")
_OPERATOR = Operator(
    sub="secret-sink-test",
    name=None,
    email=None,
    raw_jwt="<secret-sink-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_EXISTING_USER = {"id": _USER_UUID, "username": _USERNAME, "enabled": True}


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
            secret_ref="rdc-hetzner-dc/keycloak/admin",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "26.0.5"},
            notes="seeded by test_secret_broker_keycloak_sink",
        )
        session.add(target)
        await session.commit()


@pytest.fixture
async def keycloak_sink_env(
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
# Registration — the keycloak kind is on the T1 registry
# ---------------------------------------------------------------------------


def test_keycloak_kind_registered_in_secret_registry() -> None:
    """The keycloak sink registers under kind ``"keycloak"`` at import time."""
    assert SECRET_ENDPOINT_REGISTRY.get("keycloak") is KeycloakCredentialSecretEndpoint


# ---------------------------------------------------------------------------
# Sink-only — keycloak credentials cannot be read back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_source_read_is_unsupported() -> None:
    """``read_secret`` on the keycloak kind raises — it is a write-only sink."""
    endpoint = SECRET_ENDPOINT_REGISTRY["keycloak"](f"{_TARGET_NAME}/{_REALM}/{_USERNAME}#password")
    with pytest.raises(NotImplementedError, match="write-only"):
        await endpoint.read_secret(_OPERATOR)


# ---------------------------------------------------------------------------
# Ref grammar — malformed / unsupported-field rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "target/realm/user",  # missing #field
        "target/realm/user#secret",  # unsupported field
        "target/realm#password",  # too few segments
        "target/realm/user/extra#password",  # too many segments
        "target//user#password",  # empty segment
    ],
    ids=["no-field", "bad-field", "too-few", "too-many", "empty-segment"],
)
def test_keycloak_ref_rejects_malformed(ref: str) -> None:
    with pytest.raises(KeycloakSecretRefError):
        KeycloakCredentialSecretEndpoint(ref)


# ---------------------------------------------------------------------------
# Cross-kind move — value reaches Keycloak, never the caller/audit/logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vault_to_keycloak_move_writes_credential_server_side(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    keycloak_sink_env: KeycloakConnector,
) -> None:
    """A vault: → keycloak: move sets the user password; value stays server-side.

    Asserts the sentinel appears ONLY in the mocked Admin REST
    reset-password PUT body — not in the op response, the op params, the
    captured logs, or the persisted audit row.
    """
    install_fake_client(monkeypatch, secret={"password": _SENTINEL})

    params = {
        "from": "vault:secret/db/prod#password",
        "to": f"keycloak:{_TARGET_NAME}/{_REALM}/{_USERNAME}#password",
        "reason": "provision keycloak operator credential",
    }

    with (
        caplog.at_level(logging.DEBUG),
        respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock,
    ):
        _mount_admin_token(mock)
        list_route = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[_EXISTING_USER])
        put_route = mock.put(f"/admin/realms/{_REALM}/users/{_USER_UUID}/reset-password").respond(
            204
        )
        result = await _dispatch_move(params)

    assert result.status == "ok", result.error

    # (a) The sink resolved username→UUID and PUT the credential.
    assert list_route.called, "should have looked up the username → UUID"
    assert put_route.called, "should have PUT the reset-password credential"

    # (b) The sentinel value rode into the Admin REST PUT body...
    put_body = json.loads(put_route.calls.last.request.content)
    assert put_body == {"type": "password", "value": _SENTINEL, "temporary": False}

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
