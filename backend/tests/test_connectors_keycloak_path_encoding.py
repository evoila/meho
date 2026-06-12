# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Path-segment encoding + UUID-schema regression tests for the Keycloak connector.

Verifies the fix from G0.17-T1 #96:

1. ``quote_segment`` encodes traversal-shaped ids so they cannot alter the
   request path structure (criterion 1 / 2 from the issue).
2. A traversal id passed to ``keycloak_client_get`` issues a request whose
   path stays under ``realms/{managed_realm}/`` — the ``..`` segments are
   encoded and cannot climb out (criterion 3).
3. The same traversal id is rejected **before** the handler runs by the
   JSON-schema UUID pattern gate — no transport request is recorded
   (criterion 5).
4. The helper is present in the keycloak package and matches the ArgoCD
   precedent (criterion 2).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import respx

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_read import (
    _UUID_PATTERN,
    keycloak_client_get,
    keycloak_role_mapping_get,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
    quote_segment,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "rdc-keycloak-encoding-test"
_KC_HOST = "keycloak-encoding.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_TOKEN = "kc-admin-token-encoding"
_CLIENT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_TRAVERSAL_ID = "../../../../realms/master/clients"
_TRAVERSAL_ENCODED = "%2E%2E%2F%2E%2E%2F%2E%2E%2F%2E%2E%2Frealms%2Fmaster%2Fclients"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000096")
_OPERATOR = Operator(
    sub="keycloak-encoding-test",
    name="Keycloak Encoding Test Operator",
    email=None,
    raw_jwt="<keycloak-encoding-test-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)


# ---------------------------------------------------------------------------
# Unit: quote_segment helper (criteria 1, 2)
# ---------------------------------------------------------------------------


def test_quote_segment_encodes_slash_and_dot() -> None:
    """quote_segment encodes '/' and '.' so neither slash nor dot-segment traversal can form."""
    encoded = quote_segment("../../../../realms/master/clients")
    assert "/" not in encoded
    assert "." not in encoded
    assert encoded == "%2E%2E%2F%2E%2E%2F%2E%2E%2F%2E%2E%2Frealms%2Fmaster%2Fclients"


def test_quote_segment_encodes_bare_dot_segment() -> None:
    """A bare '..' id is encoded to '%2E%2E' and cannot be normalised by httpx."""
    assert quote_segment("..") == "%2E%2E"
    assert quote_segment(".") == "%2E"


def test_quote_segment_leaves_normal_uuid_unchanged() -> None:
    """A well-formed UUID is unchanged (contains no characters that need encoding)."""
    uuid_val = "11111111-1111-1111-1111-111111111111"
    assert quote_segment(uuid_val) == uuid_val


def test_quote_segment_accepts_any_type_via_str_coercion() -> None:
    """quote_segment coerces non-string values to str before encoding."""
    assert quote_segment(42) == "42"


def test_quote_segment_extends_argocd_precedent_with_dot_encoding() -> None:
    """quote_segment extends the ArgoCD _quote_name pattern by also encoding '.'.

    ArgoCD _quote_name uses ``quote(str(v), safe="")``, which leaves '.' unencoded
    because RFC 3986 lists it as an unreserved character.  The Keycloak connector
    tightens this by additionally encoding '.' so that bare '..' dot-segments cannot
    be normalised away by httpx at request-build time.

    Inputs without '.' still match the ArgoCD output exactly; inputs with '.' differ
    in that every '.' is replaced with '%2E'.
    """
    from urllib.parse import quote

    # Values without dots: output must match the ArgoCD primitive.
    no_dot_cases = [
        "valid-uuid-1234",
        "name with spaces",
        "name/with/slashes",
    ]
    for val in no_dot_cases:
        assert quote_segment(val) == quote(str(val), safe=""), f"mismatch for {val!r}"

    # Values with dots: quote_segment encodes '.' while ArgoCD does not.
    assert quote_segment("..") == "%2E%2E"
    assert quote_segment("../../../../realms/master") == (
        quote("../../../../realms/master", safe="").replace(".", "%2E")
    )


# ---------------------------------------------------------------------------
# Unit: path-interpolation uses encoded segment (criterion 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_get_encodes_traversal_id_in_request_path() -> None:
    """keycloak_client_get with a traversal id issues a request under realms/{managed_realm}.

    Uses an httpx MockTransport to capture the built request URL and assert
    that the path stays confined to ``/admin/realms/evba/clients/`` with the
    traversal chars percent-encoded, not resolved as ``..`` segments.
    """

    async def _stub_loader(
        _target: KeycloakTargetLike, _operator: Operator
    ) -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    connector = KeycloakConnector(credentials_loader=_stub_loader)

    # Inject the mock transport into the connector's HTTP client.
    target_stub = MagicMock(spec=KeycloakTargetLike)
    target_stub.name = "encoding-test-target"
    target_stub.host = "keycloak-encoding.test.invalid"
    target_stub.port = 443
    target_stub.secret_ref = "stub/secret"
    target_stub.auth_model = "shared_service_account"
    target_stub.extras = {}
    # Tenant-unique cache key components (#1642/#1672); a MagicMock with a
    # Protocol spec does not auto-expose annotation-only attributes, so set
    # them explicitly or target_cache_key raises AttributeError.
    target_stub.id = uuid.UUID(int=0xCE)
    target_stub.tenant_id = uuid.UUID(int=0)

    # Patch the connector to use our mock transport via respx.
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
        )
        # The traversal-encoded path should be what reaches the transport.
        encoded_path = f"/admin/realms/evba/clients/{_TRAVERSAL_ENCODED}"
        route = mock.get(encoded_path).respond(
            200, json={"id": _CLIENT_UUID, "clientId": "meho-backplane"}
        )

        result = await keycloak_client_get(connector, _OPERATOR, target_stub, {"id": _TRAVERSAL_ID})

    # The route was called — meaning the path stayed under realms/evba/clients/
    # with the traversal chars encoded, and was NOT resolved as ../../ by httpx.
    assert route.called, f"Expected GET {encoded_path} to be called; traversal id was not encoded."
    assert result["client"]["id"] == _CLIENT_UUID

    await connector.aclose()


@pytest.mark.asyncio
async def test_role_mapping_get_encodes_traversal_id_in_request_path() -> None:
    """keycloak_role_mapping_get with a traversal id stays under realms/{managed_realm}."""

    async def _stub_loader(
        _target: KeycloakTargetLike, _operator: Operator
    ) -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    connector = KeycloakConnector(credentials_loader=_stub_loader)

    target_stub = MagicMock(spec=KeycloakTargetLike)
    target_stub.name = "encoding-test-target-role"
    target_stub.host = "keycloak-encoding.test.invalid"
    target_stub.port = 443
    target_stub.secret_ref = "stub/secret"
    target_stub.auth_model = "shared_service_account"
    target_stub.extras = {}
    # Tenant-unique cache key components (#1642/#1672); a MagicMock with a
    # Protocol spec does not auto-expose annotation-only attributes, so set
    # them explicitly or target_cache_key raises AttributeError.
    target_stub.id = uuid.UUID(int=0xCE)
    target_stub.tenant_id = uuid.UUID(int=0)

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
        )
        encoded_path = f"/admin/realms/evba/users/{_TRAVERSAL_ENCODED}/role-mappings"
        route = mock.get(encoded_path).respond(
            200, json={"realmMappings": [], "clientMappings": {}}
        )

        result = await keycloak_role_mapping_get(
            connector, _OPERATOR, target_stub, {"id": _TRAVERSAL_ID}
        )

    assert route.called, f"Expected GET {encoded_path} to be called; traversal id was not encoded."
    assert "role_mappings" in result

    await connector.aclose()


# ---------------------------------------------------------------------------
# Schema gate: traversal id rejected before handler runs (criterion 5)
# ---------------------------------------------------------------------------


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


def _stub_loader_sync(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _seed_encoding_target() -> TargetORM:
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
            notes="seeded by test_connectors_keycloak_path_encoding",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_encoding_connector() -> KeycloakConnector:
    instance = KeycloakConnector(credentials_loader=_stub_loader_sync)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    return instance


@pytest.fixture
async def keycloak_encoding_e2e() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_encoding_target()
    connector = _wire_encoding_connector()
    yield connector
    await connector.aclose()


@pytest.mark.asyncio
async def test_schema_gate_rejects_traversal_id_before_handler(
    keycloak_encoding_e2e: KeycloakConnector,
) -> None:
    """A traversal-shaped id is rejected at the schema gate; no transport call fires.

    Dispatches ``keycloak.client.get`` with ``params={"id": traversal_id}``
    through the full ``call_operation`` stack with respx active and asserting
    all routes were called. The UUID pattern on the ``id`` property must reject
    the traversal string before the handler reaches the HTTP layer, so the
    respx mock records zero calls against the Keycloak host.
    """
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        # Only register the token route; any admin GET would be an unmatched request.
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
        )

        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client.get",
                "target": {"name": _TARGET_NAME},
                "params": {"id": _TRAVERSAL_ID},
            },
        )

        # Assert no admin REST calls were made (only token calls are registered;
        # any /admin/ call would have raised ConnectionError in the mock).
        admin_calls = [c for c in mock.calls if "/admin/" in str(c.request.url)]

    # The dispatch must fail at validation — not reach the HTTP layer.
    assert result["status"] != "ok", (
        f"Expected schema validation to reject traversal id, got status={result['status']!r}"
    )
    assert not admin_calls, (
        "The handler must not have issued any admin REST request — "
        "the schema gate should have blocked before the handler ran."
    )


@pytest.mark.asyncio
async def test_schema_gate_rejects_traversal_id_role_mapping(
    keycloak_encoding_e2e: KeycloakConnector,
) -> None:
    """A traversal id is rejected by the UUID gate on keycloak.role_mapping.get too."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
        )

        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.role_mapping.get",
                "target": {"name": _TARGET_NAME},
                "params": {"id": _TRAVERSAL_ID},
            },
        )

        admin_calls = [c for c in mock.calls if "/admin/" in str(c.request.url)]

    assert result["status"] != "ok"
    assert not admin_calls


@pytest.mark.asyncio
async def test_schema_gate_accepts_valid_uuid(
    keycloak_encoding_e2e: KeycloakConnector,
) -> None:
    """A valid UUID passes the schema gate and reaches the handler normally."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
        )
        mock.get(f"/admin/realms/evba/clients/{_CLIENT_UUID}").respond(
            200, json={"id": _CLIENT_UUID, "clientId": "meho-backplane"}
        )

        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client.get",
                "target": {"name": _TARGET_NAME},
                "params": {"id": _CLIENT_UUID},
            },
        )

    assert result["status"] == "ok", f"valid UUID rejected unexpectedly: {result.get('error')}"


# ---------------------------------------------------------------------------
# UUID pattern constant sanity
# ---------------------------------------------------------------------------


def test_uuid_pattern_rejects_traversal() -> None:
    """_UUID_PATTERN rejects a traversal-shaped id."""
    import re

    assert not re.match(_UUID_PATTERN, _TRAVERSAL_ID)


def test_uuid_pattern_accepts_valid_uuid() -> None:
    """_UUID_PATTERN accepts a well-formed UUID."""
    import re

    assert re.match(_UUID_PATTERN, _CLIENT_UUID)
    assert re.match(_UUID_PATTERN, "00000000-0000-0000-0000-000000000000")
    assert re.match(_UUID_PATTERN, "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF")


def test_uuid_pattern_rejects_non_uuid_strings() -> None:
    """_UUID_PATTERN rejects plain strings, empty strings, and SQL injection."""
    import re

    rejects = ["", "not-a-uuid", "1234", "' OR 1=1 --", "admin", "../.."]
    for val in rejects:
        assert not re.match(_UUID_PATTERN, val), f"Expected {val!r} to be rejected"


# ---------------------------------------------------------------------------
# Acceptance criterion 1: grep-equivalent check that no raw interpolation remains
# ---------------------------------------------------------------------------


def test_quote_segment_imported_and_called_in_both_handler_modules() -> None:
    """Verify quote_segment is imported and called in both handler modules.

    This is the programmatic equivalent of acceptance criterion 2's grep:
    ``grep -rn 'quote' backend/src/meho_backplane/connectors/keycloak/``

    Uses exact call-site counts (not >=) so that any future addition or removal
    of a call site triggers an explicit review.  Current counts:

    * ops_read: 2 sites — ``keycloak_client_get`` + ``keycloak_role_mapping_get``.
    * ops_write: 4 sites — ``keycloak_client_update``, ``keycloak_protocol_mapper_create``,
      ``keycloak_user_password_reset``, ``keycloak_role_mapping_add``.
    """
    import inspect

    import meho_backplane.connectors.keycloak.ops_read as ops_read_mod
    import meho_backplane.connectors.keycloak.ops_write as ops_write_mod

    expected_counts = {
        ops_read_mod: 2,
        ops_write_mod: 4,
    }
    for mod, expected in expected_counts.items():
        source = inspect.getsource(mod)
        # quote_segment must be imported.
        assert "quote_segment" in source, (
            f"quote_segment not found in {mod.__name__} — path-segment encoding is missing"
        )
        # Exact call-site count — regression guard against silently adding/removing sites.
        call_count = source.count("quote_segment(")
        assert call_count == expected, (
            f"quote_segment() called {call_count} time(s) in {mod.__name__}, "
            f"expected exactly {expected}. Update this assertion if a new call site "
            "is intentionally added or removed."
        )
