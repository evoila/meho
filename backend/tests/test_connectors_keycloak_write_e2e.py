# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.13-T4 Keycloak write-op recorded-fixture E2E test (#1406).

Drives the nine approval-gated keycloak write ops through the full
``call_operation`` dispatch stack against a respx-mocked Keycloak Admin
REST API — no running Keycloak, no live Vault. The admin-credential loader
and the user-password Vault read are both stubbed; ``respx`` replays the
Admin REST create/update fixtures; the connector instance is preseeded into
the dispatcher's instance cache.

Acceptance criteria verified (Issue #1406)
==========================================

(a) The nine write ops register with the stated safety levels, all
    ``requires_approval=True`` (asserted on ``WRITE_OPS``).
(b) Writes resolve name→UUID: ``client.update`` /
    ``protocol_mapper.create`` resolve a clientId to its UUID via
    ``?clientId=``; ``user.reset_password`` / ``role_mapping.assign``
    resolve a username to its UUID.
(c) Writes are idempotent: a 409 already-exists on a create is treated as
    success (``conflict=true``) with the existing object's UUID resolved.
(d) ``user.create`` / ``user.reset_password`` never carry the password in
    op params (it is sourced from Vault) and the password never appears in
    the result, the audit params view, or the redacted broadcast payload.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "rdc-keycloak-write-e2e"
_KC_HOST = "keycloak-write-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_TOKEN = "kc-admin-token-write-e2e"
_CLIENT_UUID = "33333333-3333-3333-3333-333333333333"
_USER_UUID = "44444444-4444-4444-4444-444444444444"
_NEW_CLIENT_UUID = "55555555-5555-5555-5555-555555555555"
_NEW_USER_UUID = "66666666-6666-6666-6666-666666666666"

#: The password sentinel the Vault stub returns. The whole point of the
#: security assertion is that this string never escapes into the op
#: result, the audit params view, or the broadcast payload.
_PASSWORD_SENTINEL = "vault-sourced-password-DO-NOT-LEAK"
_PASSWORD_SECRET_REF = "rdc-hetzner-dc/keycloak/operator-a-password"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ae")
_OPERATOR = Operator(
    sub="keycloak-write-e2e-test",
    name="Keycloak Write E2E Test Operator",
    email=None,
    raw_jwt="<keycloak-write-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

EXPECTED_WRITE_OP_IDS: tuple[str, ...] = (
    "keycloak.realm.create",
    "keycloak.realm.update",
    "keycloak.client.create",
    "keycloak.client.update",
    "keycloak.client_scope.create",
    "keycloak.protocol_mapper.create",
    "keycloak.user.create",
    "keycloak.user.reset_password",
    "keycloak.role_mapping.assign",
)

#: Expected (safety_level, requires_approval) per op — the registration
#: contract the issue's op table pins.
EXPECTED_SAFETY: dict[str, str] = {
    "keycloak.realm.create": "dangerous",
    "keycloak.realm.update": "caution",
    "keycloak.client.create": "caution",
    "keycloak.client.update": "caution",
    "keycloak.client_scope.create": "caution",
    "keycloak.protocol_mapper.create": "caution",
    "keycloak.user.create": "caution",
    "keycloak.user.reset_password": "caution",
    "keycloak.role_mapping.assign": "dangerous",
}


# ---------------------------------------------------------------------------
# Recorded Admin REST fixtures
# ---------------------------------------------------------------------------

_EXISTING_CLIENT = {"id": _CLIENT_UUID, "clientId": "meho-web", "enabled": True}
_EXISTING_USER = {"id": _USER_UUID, "username": "operator-a", "enabled": True}
_REALM_ROLE = {"id": "role-uuid-9", "name": "tenant_admin", "composite": False}


def _location(path: str) -> dict[str, str]:
    """Build a Location response header for a Keycloak create."""
    return {"Location": f"{_KC_BASE_URL}{path}"}


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
    )


# ---------------------------------------------------------------------------
# Stub credential loader + Vault password reader + seeded connector
# ---------------------------------------------------------------------------


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _stub_read_password(_operator: Operator, params: dict[str, Any]) -> str:
    """Stand in for the operator-context Vault read.

    Asserts the password is sourced via a Vault *path*, never inline: the
    op params must carry ``password_secret_ref`` and must NOT carry an
    inline ``password``.
    """
    assert params.get("password_secret_ref") == _PASSWORD_SECRET_REF
    assert "password" not in params, "password must never be an inline op param"
    return _PASSWORD_SENTINEL


async def _seed_keycloak_target() -> TargetORM:
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
            notes="seeded by test_connectors_keycloak_write_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> KeycloakConnector:
    instance = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    return instance


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def keycloak_write_e2e(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    # Stub the operator-context Vault read so no live Vault is needed.
    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.ops_write._read_password_from_vault",
        _stub_read_password,
    )
    await KeycloakConnector.register_operations()
    await _seed_keycloak_target()
    connector = _wire_seeded_connector()
    yield connector
    await connector.aclose()


async def _dispatch(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a write op, bypassing the approval gate.

    Every write registers ``requires_approval=True``; an ordinary
    ``call_operation`` would park the call in the approval queue
    (``status=awaiting_approval``) and never reach the handler. ``_approved=True``
    is the resume-path flag the approvals API sets after a human approves —
    it lets the E2E drive the handler/audit/broadcast path that runs once a
    write is authorised. The target is resolved by name the same way
    ``call_operation`` resolves it.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=resolved_target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# Registration contract (acceptance criterion a)
# ---------------------------------------------------------------------------


def test_write_ops_registration_set() -> None:
    """WRITE_OPS carries exactly the nine write ops the issue lists."""
    op_ids = {op.op_id for op in WRITE_OPS}
    assert op_ids == set(EXPECTED_WRITE_OP_IDS)
    assert len(WRITE_OPS) == 9


def test_write_ops_safety_levels_and_approval() -> None:
    """Every write op has the stated safety level and requires_approval=True."""
    for op in WRITE_OPS:
        assert op.requires_approval is True, f"{op.op_id} must require approval"
        assert op.safety_level == EXPECTED_SAFETY[op.op_id], (
            f"{op.op_id} safety_level={op.safety_level} != {EXPECTED_SAFETY[op.op_id]}"
        )
        assert "write" in op.tags, f"{op.op_id} should carry the write tag"


def test_idp_create_is_deferred() -> None:
    """idp.create is deliberately deferred — no idp write op is registered."""
    assert not any("idp" in op.op_id for op in WRITE_OPS)


# ---------------------------------------------------------------------------
# Name → UUID resolution (acceptance criterion b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_update_resolves_clientid_to_uuid(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """client.update resolves a human clientId to its internal UUID before the PUT."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        list_route = mock.get("/admin/realms/evba/clients").respond(200, json=[_EXISTING_CLIENT])
        put_route = mock.put(f"/admin/realms/evba/clients/{_CLIENT_UUID}").respond(204)
        result = await _dispatch(
            "keycloak.client.update",
            {"client_id": "meho-web", "representation": {"enabled": False}},
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["id"] == _CLIENT_UUID
    assert list_route.called, "should have looked up the clientId → UUID"
    assert put_route.called, "should have PUT against the resolved UUID"


@pytest.mark.asyncio
async def test_protocol_mapper_create_resolves_clientid(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """protocol_mapper.create resolves clientId → UUID and POSTs the mapper."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get("/admin/realms/evba/clients").respond(200, json=[_EXISTING_CLIENT])
        post_route = mock.post(
            f"/admin/realms/evba/clients/{_CLIENT_UUID}/protocol-mappers/models"
        ).respond(201, headers=_location(f"/admin/realms/evba/clients/{_CLIENT_UUID}"))
        result = await _dispatch(
            "keycloak.protocol_mapper.create",
            {
                "client_id": "meho-web",
                "representation": {
                    "name": "tenant_id",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-usermodel-attribute-mapper",
                    "config": {"claim.name": "tenant_id"},
                },
            },
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["client_uuid"] == _CLIENT_UUID
    assert result["result"]["mapper_name"] == "tenant_id"
    assert post_route.called


@pytest.mark.asyncio
async def test_role_mapping_assign_resolves_username_and_role(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """role_mapping.assign resolves username→UUID and role-name→representation."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get("/admin/realms/evba/users").respond(200, json=[_EXISTING_USER])
        mock.get("/admin/realms/evba/roles/tenant_admin").respond(200, json=_REALM_ROLE)
        post_route = mock.post(
            f"/admin/realms/evba/users/{_USER_UUID}/role-mappings/realm"
        ).respond(204)
        result = await _dispatch(
            "keycloak.role_mapping.assign",
            {"username": "operator-a", "roles": ["tenant_admin"]},
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["id"] == _USER_UUID
    assert result["result"]["assigned_roles"] == ["tenant_admin"]
    # The POST body carried the resolved RoleRepresentation, not the name.
    body = post_route.calls.last.request.content
    assert b"role-uuid-9" in body


# ---------------------------------------------------------------------------
# Idempotency (acceptance criterion c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_create_409_is_idempotent(keycloak_write_e2e: KeycloakConnector) -> None:
    """A 409 already-exists on client.create is a success; the UUID is resolved."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms/evba/clients").respond(409, json={"errorMessage": "exists"})
        mock.get("/admin/realms/evba/clients").respond(200, json=[_EXISTING_CLIENT])
        result = await _dispatch(
            "keycloak.client.create",
            {"representation": {"clientId": "meho-web"}},
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["conflict"] is True
    assert result["result"]["created"] is False
    assert result["result"]["id"] == _CLIENT_UUID


@pytest.mark.asyncio
async def test_client_create_201_returns_new_uuid(keycloak_write_e2e: KeycloakConnector) -> None:
    """A fresh client.create parses the new UUID out of the Location header."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms/evba/clients").respond(
            201, headers=_location(f"/admin/realms/evba/clients/{_NEW_CLIENT_UUID}")
        )
        result = await _dispatch(
            "keycloak.client.create",
            {"representation": {"clientId": "meho-fresh"}},
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["created"] is True
    assert result["result"]["conflict"] is False
    assert result["result"]["id"] == _NEW_CLIENT_UUID


@pytest.mark.asyncio
async def test_realm_create_409_is_idempotent(keycloak_write_e2e: KeycloakConnector) -> None:
    """A 409 already-exists on realm.create is a success (conflict=true)."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms").respond(409, json={"errorMessage": "exists"})
        result = await _dispatch(
            "keycloak.realm.create",
            {"representation": {"realm": "evba", "enabled": True}},
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["conflict"] is True
    assert result["result"]["realm"] == "evba"


# ---------------------------------------------------------------------------
# Password handling (acceptance criterion d) — critical security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_create_sources_password_from_vault_never_leaks(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """user.create sources the password from Vault; it never leaks into the result."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        post_route = mock.post("/admin/realms/evba/users").respond(
            201, headers=_location(f"/admin/realms/evba/users/{_NEW_USER_UUID}")
        )
        result = await _dispatch(
            "keycloak.user.create",
            {
                "representation": {"username": "operator-b", "enabled": True},
                "password_secret_ref": _PASSWORD_SECRET_REF,
            },
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["id"] == _NEW_USER_UUID
    assert result["result"]["created"] is True
    # The password is in the create body sent to Keycloak (expected) ...
    assert _PASSWORD_SENTINEL.encode() in post_route.calls.last.request.content
    # ... but NEVER in the op result returned to the caller / audit / broadcast.
    assert _PASSWORD_SENTINEL not in str(result)


@pytest.mark.asyncio
async def test_user_reset_password_resolves_username_and_no_leak(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """user.reset_password resolves username→UUID and never leaks the password."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get("/admin/realms/evba/users").respond(200, json=[_EXISTING_USER])
        put_route = mock.put(f"/admin/realms/evba/users/{_USER_UUID}/reset-password").respond(204)
        result = await _dispatch(
            "keycloak.user.reset_password",
            {"username": "operator-a", "password_secret_ref": _PASSWORD_SECRET_REF},
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["id"] == _USER_UUID
    assert result["result"]["password_reset"] is True
    assert _PASSWORD_SENTINEL.encode() in put_route.calls.last.request.content
    assert _PASSWORD_SENTINEL not in str(result)


def test_user_credential_ops_classify_credential_write() -> None:
    """The password-touching ops collapse to aggregate-only on the broadcast feed."""
    for op_id in ("keycloak.user.create", "keycloak.user.reset_password"):
        assert classify_op(op_id) == "credential_write"
        # The redacted broadcast payload never carries params (aggregate-only).
        payload = redact_payload(
            "credential_write",
            {"password_secret_ref": _PASSWORD_SECRET_REF, "username": "operator-a"},
            "ok",
        )
        assert "params" not in payload
        assert payload == {"op_class": "credential_write", "result_status": "ok"}


def test_role_mapping_assign_classifies_write() -> None:
    """role_mapping.assign classifies as a plain write (mutation feed signal)."""
    assert classify_op("keycloak.role_mapping.assign") == "write"


# ---------------------------------------------------------------------------
# Admin-token-only invariant + search visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_use_admin_token_never_operator_jwt(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """Every write carries the admin Bearer, never the operator JWT."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms").respond(201, headers=_location("/admin/realms/evba"))
        await _dispatch("keycloak.realm.create", {"representation": {"realm": "evba"}})
        for call in mock.calls:
            req = call.request
            if req.url.path.startswith("/admin/"):
                assert req.headers.get("authorization") == f"Bearer {_ADMIN_TOKEN}"
            assert _OPERATOR.raw_jwt not in (req.headers.get("authorization") or "")


@pytest.mark.asyncio
async def test_write_ops_visible_to_search_operations(
    keycloak_write_e2e: KeycloakConnector,
) -> None:
    """All nine write ops are discoverable via search_operations."""
    result = await search_operations(
        _OPERATOR,
        {
            "connector_id": _CONNECTOR_ID,
            "query": "keycloak create update realm client user role password",
            "limit": 50,
        },
    )
    surfaced = {hit["op_id"] for hit in result["hits"]}
    missing = set(EXPECTED_WRITE_OP_IDS) - surfaced
    assert not missing, f"search_operations did not surface: {missing}"
