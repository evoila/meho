# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.13-T2 Keycloak recorded-fixture E2E test (#1394).

Drives every curated keycloak read op through the full ``call_operation``
dispatch stack against a respx-mocked Keycloak Admin REST API — no running
Keycloak, no live Vault. The admin-credential loader is injected (stub),
``respx`` replays pre-recorded Admin REST fixtures, and the connector
instance is preseeded into the dispatcher's instance cache so dispatch
uses the stub-loaded connector rather than a plain one that would try a
live Vault read.

Acceptance criteria verified (Issue #1394)
==========================================

(a) All 6 read ops dispatch through ``call_operation`` via the admin-auth
    path and return ``status="ok"``; all 6 are visible to
    ``search_operations``.
(b) ``keycloak.client.get`` returns the client's flows
    (``authenticationFlowBindingOverrides``), redirect URIs
    (``redirectUris``), and protocol mappers (``protocolMappers``); the
    confidential-client ``secret`` is redacted.
(c) ``keycloak.user.list`` never surfaces credential material
    (``credentials`` redacted).
(d) Every op carries ``safety_level="safe"`` + ``requires_approval=False``
    and a ``read-only`` tag (asserted on the ``READ_OPS`` table).

Fixtures reproduce realistic-but-minimal Keycloak 26.x Admin REST output,
including the secret material the redaction contract must scrub.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_read import READ_OPS
from meho_backplane.connectors.keycloak.redaction import REDACTED
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, search_operations
from meho_backplane.operations.reducer import PassThroughReducer

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "rdc-keycloak-e2e"
_KC_HOST = "keycloak-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_TOKEN = "kc-admin-token-e2e"
_CLIENT_UUID = "11111111-1111-1111-1111-111111111111"
_USER_UUID = "22222222-2222-2222-2222-222222222222"
_CLIENT_SECRET_SENTINEL = "super-secret-client-secret-DO-NOT-LEAK"
_USER_CRED_SENTINEL = "hashed-password-DO-NOT-LEAK"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ad")
_OPERATOR = Operator(
    sub="keycloak-e2e-test",
    name="Keycloak E2E Test Operator",
    email=None,
    raw_jwt="<keycloak-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

EXPECTED_OP_IDS: tuple[str, ...] = (
    "keycloak.realm.get",
    "keycloak.client.list",
    "keycloak.client.get",
    "keycloak.client_scope.list",
    "keycloak.user.list",
    "keycloak.role_mapping.get",
)


# ---------------------------------------------------------------------------
# Recorded Admin REST fixtures (minimal Keycloak 26.x shapes, with secrets)
# ---------------------------------------------------------------------------

_FIXTURE_REALM: dict[str, Any] = {
    "realm": "evba",
    "enabled": True,
    "sslRequired": "external",
    "loginTheme": "meho",
    "accessTokenLifespan": 300,
}

_FIXTURE_CLIENT: dict[str, Any] = {
    "id": _CLIENT_UUID,
    "clientId": "meho-backplane",
    "enabled": True,
    "publicClient": False,
    "secret": _CLIENT_SECRET_SENTINEL,
    "redirectUris": ["https://meho.evba.lab/callback"],
    "webOrigins": ["https://meho.evba.lab"],
    "protocolMappers": [
        {"name": "tenant_id", "protocol": "openid-connect", "config": {"claim.name": "tenant_id"}}
    ],
    "authenticationFlowBindingOverrides": {"browser": "flow-uuid-abc"},
}

_FIXTURE_CLIENTS: list[dict[str, Any]] = [_FIXTURE_CLIENT]

_FIXTURE_CLIENT_SCOPES: list[dict[str, Any]] = [
    {
        "id": "scope-uuid-1",
        "name": "roles",
        "protocol": "openid-connect",
        "protocolMappers": [{"name": "realm roles", "protocol": "openid-connect"}],
    }
]

_FIXTURE_USERS: list[dict[str, Any]] = [
    {
        "id": _USER_UUID,
        "username": "operator-a",
        "enabled": True,
        "emailVerified": True,
        # The Admin API can surface credential records on some reads; the
        # op must scrub them regardless of Keycloak's default projection.
        "credentials": [{"type": "password", "secretData": _USER_CRED_SENTINEL}],
    }
]

_FIXTURE_ROLE_MAPPINGS: dict[str, Any] = {
    "realmMappings": [{"id": "role-uuid-1", "name": "tenant_admin"}],
    "clientMappings": {
        "meho-backplane": {
            "id": _CLIENT_UUID,
            "client": "meho-backplane",
            "mappings": [{"id": "role-uuid-2", "name": "view-realm"}],
        }
    },
}


def _mount_admin_routes(mock: respx.MockRouter) -> None:
    """Register the token endpoint + 6 admin REST fixture routes on *mock*."""
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
    )
    mock.get("/admin/realms/evba").respond(200, json=_FIXTURE_REALM)
    mock.get("/admin/realms/evba/clients").respond(200, json=_FIXTURE_CLIENTS)
    mock.get(f"/admin/realms/evba/clients/{_CLIENT_UUID}").respond(200, json=_FIXTURE_CLIENT)
    mock.get("/admin/realms/evba/client-scopes").respond(200, json=_FIXTURE_CLIENT_SCOPES)
    mock.get("/admin/realms/evba/users").respond(200, json=_FIXTURE_USERS)
    mock.get(f"/admin/realms/evba/users/{_USER_UUID}/role-mappings").respond(
        200, json=_FIXTURE_ROLE_MAPPINGS
    )


# ---------------------------------------------------------------------------
# Stub credential loader + seeded connector instance
# ---------------------------------------------------------------------------


def _stub_loader(
    _target: KeycloakTargetLike, _operator: Operator
) -> Any:  # pragma: no cover - trivial
    """Return a fixed client-credentials admin credential (no live Vault)."""

    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _seed_keycloak_target() -> TargetORM:
    """Insert the E2E target row (product=keycloak, version=26.x) and return it."""
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
            notes="seeded by test_connectors_keycloak_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> KeycloakConnector:
    """Preseed a stub-loader connector into the dispatcher's instance cache."""
    instance = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance  # type: ignore[assignment]
    return instance


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars that :class:`Settings` requires for this module."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches around every test."""
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def keycloak_e2e() -> AsyncIterator[KeycloakConnector]:
    """Register the read ops, seed the target, and preseed the connector."""
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_keycloak_target()
    connector = _wire_seeded_connector()
    yield connector
    await connector.aclose()


# ---------------------------------------------------------------------------
# Op-table assertions (acceptance criterion d)
# ---------------------------------------------------------------------------


def test_keycloak_read_ops_registration_set() -> None:
    """READ_OPS carries exactly the 6 curated read ops and no write op."""
    op_ids = {op.op_id for op in READ_OPS}
    assert op_ids == set(EXPECTED_OP_IDS)
    assert len(READ_OPS) == 6


def test_keycloak_read_ops_all_safe_no_approval_read_only_tag() -> None:
    """Every op is safe, needs no approval, and carries the read-only tag."""
    for op in READ_OPS:
        assert op.safety_level == "safe", f"{op.op_id} should be safe"
        assert op.requires_approval is False, f"{op.op_id} should not require approval"
        assert "read-only" in op.tags, f"{op.op_id} should carry the read-only tag"


def test_keycloak_no_write_op_registered() -> None:
    """No op_id implies a mutating verb (create/update/delete/add/remove/set)."""
    write_verbs = ("create", "update", "delete", "add", "remove", "set", "apply", "reset")
    for op in READ_OPS:
        suffix = op.op_id.rsplit(".", 1)[-1]
        assert not any(suffix.startswith(v) for v in write_verbs), (
            f"{op.op_id} looks like a write op — T2 ships read-only"
        )


# ---------------------------------------------------------------------------
# Full dispatch path (acceptance criteria a, b, c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_e2e_realm_get(keycloak_e2e: KeycloakConnector) -> None:
    """keycloak.realm.get returns the realm config via the admin-auth path."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.realm.get",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"realm.get failed: {result.get('error')}"
    realm = result["result"]["realm"]
    assert realm["realm"] == "evba"
    assert realm["sslRequired"] == "external"


@pytest.mark.asyncio
async def test_keycloak_e2e_client_list_redacts_secret(keycloak_e2e: KeycloakConnector) -> None:
    """keycloak.client.list returns rows with the client secret redacted."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"client.list failed: {result.get('error')}"
    rows = result["result"]["rows"]
    assert result["result"]["total"] == 1
    assert rows[0]["clientId"] == "meho-backplane"
    assert rows[0]["secret"] == REDACTED
    assert _CLIENT_SECRET_SENTINEL not in str(result)


@pytest.mark.asyncio
async def test_keycloak_e2e_client_get_returns_flows_uris_mappers(
    keycloak_e2e: KeycloakConnector,
) -> None:
    """keycloak.client.get surfaces flows/redirect-URIs/mappers; secret redacted."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client.get",
                "target": {"name": _TARGET_NAME},
                "params": {"id": _CLIENT_UUID},
            },
        )
    assert result["status"] == "ok", f"client.get failed: {result.get('error')}"
    client = result["result"]["client"]
    # Acceptance: flows + redirect URIs + protocol mappers an operator reads.
    assert client["redirectUris"] == ["https://meho.evba.lab/callback"]
    assert client["protocolMappers"][0]["name"] == "tenant_id"
    assert client["authenticationFlowBindingOverrides"]["browser"] == "flow-uuid-abc"
    # Acceptance: secret redacted.
    assert client["secret"] == REDACTED
    assert _CLIENT_SECRET_SENTINEL not in str(result)


@pytest.mark.asyncio
async def test_keycloak_e2e_client_scope_list(keycloak_e2e: KeycloakConnector) -> None:
    """keycloak.client_scope.list returns scopes with their protocol mappers."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client_scope.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"client_scope.list failed: {result.get('error')}"
    rows = result["result"]["rows"]
    assert rows[0]["name"] == "roles"
    assert rows[0]["protocolMappers"][0]["name"] == "realm roles"


@pytest.mark.asyncio
async def test_keycloak_e2e_user_list_never_surfaces_credentials(
    keycloak_e2e: KeycloakConnector,
) -> None:
    """keycloak.user.list returns users with credential material redacted."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.user.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
    assert result["status"] == "ok", f"user.list failed: {result.get('error')}"
    rows = result["result"]["rows"]
    assert rows[0]["username"] == "operator-a"
    # Acceptance: credentials never surface.
    assert rows[0]["credentials"] == REDACTED
    assert _USER_CRED_SENTINEL not in str(result)


@pytest.mark.asyncio
async def test_keycloak_e2e_role_mapping_get(keycloak_e2e: KeycloakConnector) -> None:
    """keycloak.role_mapping.get returns realm + client role mappings."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.role_mapping.get",
                "target": {"name": _TARGET_NAME},
                "params": {"id": _USER_UUID},
            },
        )
    assert result["status"] == "ok", f"role_mapping.get failed: {result.get('error')}"
    mappings = result["result"]["role_mappings"]
    assert mappings["realmMappings"][0]["name"] == "tenant_admin"
    assert "meho-backplane" in mappings["clientMappings"]


@pytest.mark.asyncio
async def test_keycloak_e2e_all_ops_use_admin_token_never_operator_jwt(
    keycloak_e2e: KeycloakConnector,
) -> None:
    """Every dispatched op carries the admin Bearer, never the operator JWT."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_admin_routes(mock)
        for op_id, params in (
            ("keycloak.realm.get", {}),
            ("keycloak.client.list", {}),
            ("keycloak.client.get", {"id": _CLIENT_UUID}),
            ("keycloak.client_scope.list", {}),
            ("keycloak.user.list", {}),
            ("keycloak.role_mapping.get", {"id": _USER_UUID}),
        ):
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": _CONNECTOR_ID,
                    "op_id": op_id,
                    "target": {"name": _TARGET_NAME},
                    "params": params,
                },
            )
            assert result["status"] == "ok", f"{op_id} failed: {result.get('error')}"

        # No admin REST call carried the operator JWT; the GETs carried the
        # admin Bearer minted via the admin-credential path.
        for call in mock.calls:
            req = call.request
            if req.url.path.startswith("/admin/"):
                assert req.headers.get("authorization") == f"Bearer {_ADMIN_TOKEN}"
            assert _OPERATOR.raw_jwt not in (req.headers.get("authorization") or "")


# ---------------------------------------------------------------------------
# Admin-token-refresh through the dispatch stack (G3.13-T3 #1395)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_e2e_admin_token_refreshes_across_dispatch(
    keycloak_e2e: KeycloakConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin token is re-minted mid-stream when its TTL lapses between dispatches.

    G3.13-T3 (#1395) calls out the **dispatch + admin-token-refresh
    path** specifically: the token-refresh lifecycle observed through the
    full ``call_operation`` stack (not just a direct ``auth_headers``
    unit call). This drives two dispatched read ops with the connector's
    monotonic clock advanced past the cached token's effective TTL
    between them, and asserts:

    * the token endpoint is hit **twice** (mint, then re-mint after expiry),
    * both dispatched ops still return ``status="ok"`` (the re-mint is
      transparent to the caller), and
    * every admin REST GET still carries the admin Bearer, never the
      operator JWT (the refresh does not leak the operator token onto the
      Keycloak surface).

    The token fixture returns ``expires_in=60``; with the connector's
    30 s refresh margin the effective TTL is 30 s, so advancing the clock
    by 31 s forces a re-mint on the second dispatch.
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.connector.time.monotonic",
        lambda: clock["now"],
    )

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            # expires_in=60, refresh margin 30 -> effective TTL 30 s.
            200,
            json={"access_token": _ADMIN_TOKEN, "expires_in": 60},
        )
        mock.get("/admin/realms/evba").respond(200, json=_FIXTURE_REALM)
        mock.get("/admin/realms/evba/clients").respond(200, json=_FIXTURE_CLIENTS)

        first = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.realm.get",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
        assert first["status"] == "ok", f"first dispatch failed: {first.get('error')}"
        assert token_route.call_count == 1, "first dispatch should mint the admin token once"

        # Advance the connector's monotonic clock past the 30 s effective
        # TTL so the cached token is stale for the next dispatch.
        clock["now"] = 1000.0 + 31.0

        second = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
        assert second["status"] == "ok", f"second dispatch failed: {second.get('error')}"
        # The expired token forced a re-mint on the second dispatch.
        assert token_route.call_count == 2, "expired token should be re-minted on the next dispatch"

        # Neither dispatch leaked the operator JWT onto the Keycloak admin
        # surface; the admin GETs carried the re-minted admin Bearer.
        for call in mock.calls:
            req = call.request
            if req.url.path.startswith("/admin/"):
                assert req.headers.get("authorization") == f"Bearer {_ADMIN_TOKEN}"
            assert _OPERATOR.raw_jwt not in (req.headers.get("authorization") or "")


# ---------------------------------------------------------------------------
# search_operations visibility (acceptance criterion a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_e2e_ops_visible_to_search_operations(
    keycloak_e2e: KeycloakConnector,
) -> None:
    """All 6 read ops are discoverable via search_operations on the connector."""
    result = await search_operations(
        _OPERATOR,
        {"connector_id": _CONNECTOR_ID, "query": "keycloak realm client user role", "limit": 50},
    )
    surfaced = {hit["op_id"] for hit in result["hits"]}
    missing = set(EXPECTED_OP_IDS) - surfaced
    assert not missing, f"search_operations did not surface: {missing} (got {surfaced})"
