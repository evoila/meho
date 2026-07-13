# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`KeycloakConnector` admin-auth + fingerprint (G3.13-T1 #1393).

Exercises the load-bearing Keycloak connector contract:

* The **admin-vs-operator credential split** -- the connector mints an
  admin token via the admin credential path
  (``POST /realms/{admin_realm}/protocol/openid-connect/token``) and
  sends ``Authorization: Bearer <admin_token>`` on admin calls. The
  operator's OIDC token (``operator.raw_jwt``) is NEVER sent to Keycloak;
  a dedicated test asserts the admin token, not the operator token,
  reaches the admin surface.
* ``client_credentials`` grant for a client-id/secret admin credential
  and ``password`` grant for the break-glass username/password fallback.
* Admin token caching with TTL refresh (a fresh token is reused; an
  expired one is re-minted).
* ``fingerprint()`` round-trips ``GET /admin/realms/{managed_realm}`` and
  surfaces realm metadata + the server version from
  ``GET /admin/serverinfo``; transport failure yields ``reachable=False``.
* ``auth_model != "shared_service_account"`` (or ``None``) raises
  :exc:`NotImplementedError`.
* Empty ``operator.raw_jwt`` (system-initiated caller) fails closed.
* Versioned + wildcard dual registration.

Stubbing strategy: the credential loader is injected (no live Vault), and
``respx`` mocks the Keycloak HTTP surface. ``base_url`` is
``https://<host>`` (port 443 omitted by ``HttpConnector._base_url``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.connector import KeycloakAdminTokenError
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakPasswordCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel

_OPERATOR_JWT = "operator.oidc.token-DO-NOT-LEAK"
_ADMIN_TOKEN = "kc-admin-token-abc-123"


def _make_operator(raw_jwt: str = _OPERATOR_JWT) -> Operator:
    """Return a minimal :class:`Operator` carrying a recognisable JWT.

    The default ``raw_jwt`` is a sentinel string the split test scans the
    captured admin requests for -- it must never appear on a Keycloak
    admin call.
    """
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_keycloak_registry() -> Iterator[None]:
    """Re-register KeycloakConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before every
    test in this module and clear after -- same pattern the vRLI / NSX
    auth-test modules established.
    """
    clear_registry()
    register_connector_v2(
        product=KeycloakConnector.product,
        version=KeycloakConnector.version,
        impl_id=KeycloakConnector.impl_id,
        cls=KeycloakConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub -- satisfies KeycloakTargetLike Protocol structurally.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None = 443
    secret_ref: str | None = "rdc-hetzner-dc/keycloak/admin"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    extras: dict[str, Any] = field(default_factory=dict)
    # Tenant-unique cache key components (#1642/#1672).
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(name="keycloak-a", host="keycloak-a.test.invalid")


def _client_loader(
    creds: KeycloakAdminCredentials,
) -> Any:
    """Return an injected loader yielding a fixed credential object."""

    async def _load(_target: KeycloakTargetLike, _operator: Operator) -> KeycloakAdminCredentials:
        return creds

    return _load


def _make_connector(
    creds: KeycloakAdminCredentials | None = None,
) -> KeycloakConnector:
    """Build a connector wired with a stub credential loader."""
    creds = creds or KeycloakClientCredentials(client_id="meho-admin", client_secret="s3cret")
    return KeycloakConnector(credentials_loader=_client_loader(creds))


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_keycloak_connector_subclasses_http_connector() -> None:
    """The connector inherits from HttpConnector with the right v2 metadata."""
    assert issubclass(KeycloakConnector, HttpConnector)
    assert KeycloakConnector.product == "keycloak"
    assert KeycloakConnector.version == "26.x"
    assert KeycloakConnector.impl_id == "keycloak-admin"
    assert KeycloakConnector.supported_version_range == ">=26.0,<27.0"
    assert KeycloakConnector.priority == 1


def test_package_import_registers_versioned_plus_wildcard() -> None:
    """Importing the package registers BOTH versioned and wildcard v2 entries.

    Drives the registry clear + reload itself so the assertion observes
    the side-effect of importing the package rather than the autouse
    fixture's re-registration -- the reload pattern the bind9 sibling
    test uses for the same reason.
    """
    import importlib

    import meho_backplane.connectors.keycloak as keycloak_pkg

    clear_registry()
    importlib.reload(keycloak_pkg)

    v2 = all_connectors_v2()
    assert v2[("keycloak", "26.x", "keycloak-admin")] is KeycloakConnector
    # G0.15-T6 (#1215) wildcard fanout -- a fresh target with version=None
    # resolves through the wildcard.
    assert v2[("keycloak", "", "")] is KeycloakConnector


# ---------------------------------------------------------------------------
# Admin-vs-operator credential split (the load-bearing assertion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_token_not_operator_token_used_on_admin_calls() -> None:
    """The operator-OIDC token is NEVER sent to Keycloak; the admin token is.

    This is the acceptance-criterion test for the admin-vs-operator
    split. The connector mints an admin token via the token endpoint and
    sends it as the Bearer on the admin realm GET; the operator's JWT
    (used only to authorise the Vault credential read) must not appear on
    any request to Keycloak.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 60, "token_type": "Bearer"}
        )
        realm_route = mock.get("/admin/realms/evba").respond(
            200, json={"realm": "evba", "enabled": True, "sslRequired": "external"}
        )
        mock.get("/admin/serverinfo").respond(200, json={"systemInfo": {"version": "26.0.5"}})
        fp = await connector.fingerprint(_TARGET, operator=_make_operator())

    assert fp.reachable is True

    # The admin realm GET carried the admin token, NOT the operator token.
    assert realm_route.called
    realm_auth = realm_route.calls[0].request.headers.get("authorization")
    assert realm_auth == f"Bearer {_ADMIN_TOKEN}"
    assert _OPERATOR_JWT not in (realm_auth or "")

    # The token POST itself carried no stale Authorization header (the
    # grant credentials live in the form body) and never the operator JWT.
    token_req = token_route.calls[0].request
    assert "authorization" not in {k.lower() for k in token_req.headers}

    # Defensive: the operator JWT appears on NO captured Keycloak request.
    for call in mock.calls:
        for value in call.request.headers.values():
            assert _OPERATOR_JWT not in value
        assert _OPERATOR_JWT not in call.request.read().decode(errors="ignore")

    await connector.aclose()


@pytest.mark.asyncio
async def test_client_credentials_grant_form_body() -> None:
    """A client-id/secret credential mints via the client_credentials grant."""
    connector = _make_connector(
        KeycloakClientCredentials(client_id="meho-admin", client_secret="s3cret")
    )

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 60}
        )
        mock.get("/admin/realms/evba").respond(200, json={"realm": "evba", "enabled": True})
        mock.get("/admin/serverinfo").respond(404)
        await connector.fingerprint(_TARGET, operator=_make_operator())

    body = token_route.calls[0].request.read().decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=meho-admin" in body
    assert "client_secret=s3cret" in body
    ctype = token_route.calls[0].request.headers.get("content-type", "")
    assert ctype.startswith("application/x-www-form-urlencoded")

    await connector.aclose()


@pytest.mark.asyncio
async def test_password_grant_form_body() -> None:
    """A username/password credential mints via the password grant on admin-cli."""
    connector = _make_connector(KeycloakPasswordCredentials(username="admin", password="pw"))

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 60}
        )
        mock.get("/admin/realms/evba").respond(200, json={"realm": "evba", "enabled": True})
        mock.get("/admin/serverinfo").respond(404)
        await connector.fingerprint(_TARGET, operator=_make_operator())

    body = token_route.calls[0].request.read().decode()
    assert "grant_type=password" in body
    assert "client_id=admin-cli" in body
    assert "username=admin" in body
    assert "password=pw" in body

    await connector.aclose()


# ---------------------------------------------------------------------------
# Token caching + TTL refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_token_cached_across_calls() -> None:
    """A fresh admin token is reused -- the token endpoint is hit once."""
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 3600}
        )
        h1 = await connector.auth_headers(_TARGET, operator=_make_operator())
        h2 = await connector.auth_headers(_TARGET, operator=_make_operator())

    assert h1 == h2 == {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
    assert token_route.call_count == 1

    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_tokens() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached admin token.

    Regression guard for #1642/#1672: the admin-token cache used to key on
    ``target.name`` alone, so two same-named targets in different tenants
    collapsed onto one entry and one tenant could be served another
    tenant's token. The cache keys on the tenant-unique ``(tenant_id, id)``
    tuple instead. Both stub targets share one host so the established
    admin token, not the per-target HTTP-client pool, is under test.
    """
    connector = _make_connector()
    tenant_one = _StubTarget(
        name="keycloak-shared",
        host="keycloak-shared.test.invalid",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="keycloak-shared",
        host="keycloak-shared.test.invalid",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    async with respx.mock(base_url="https://keycloak-shared.test.invalid") as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 3600}
        )
        await connector.auth_headers(tenant_one, operator=_make_operator())
        await connector.auth_headers(tenant_two, operator=_make_operator())

    # Each tenant minted its own token — no cross-tenant cache hit.
    assert token_route.call_count == 2
    assert set(connector._admin_tokens) == {
        target_cache_key(tenant_one),
        target_cache_key(tenant_two),
    }

    # Same-tenant re-fetch is a cache HIT — token endpoint not re-hit.
    async with respx.mock(
        base_url="https://keycloak-shared.test.invalid", assert_all_called=False
    ) as mock:
        again = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "different", "expires_in": 3600}
        )
        await connector.auth_headers(tenant_one, operator=_make_operator())
        assert again.call_count == 0

    await connector.aclose()


@pytest.mark.asyncio
async def test_admin_token_re_minted_when_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired cached token is re-minted on the next call.

    Drives the TTL path by advancing the monotonic clock the connector
    caches against past the token's effective expiry.
    """
    connector = _make_connector()
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.connector.time.monotonic",
        lambda: clock["now"],
    )

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        token_route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            # expires_in=60, refresh margin 30 -> effective TTL 30s.
            200,
            json={"access_token": _ADMIN_TOKEN, "expires_in": 60},
        )
        await connector.auth_headers(_TARGET, operator=_make_operator())
        # Advance past the 30s effective TTL.
        clock["now"] = 1000.0 + 31.0
        await connector.auth_headers(_TARGET, operator=_make_operator())

    assert token_route.call_count == 2

    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_surfaces_realm_metadata_and_version() -> None:
    """fingerprint() returns realm metadata + server version under extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 60}
        )
        mock.get("/admin/realms/evba").respond(
            200,
            json={
                "realm": "evba",
                "enabled": True,
                "sslRequired": "external",
                "loginTheme": "meho",
            },
        )
        mock.get("/admin/serverinfo").respond(200, json={"systemInfo": {"version": "26.0.5"}})
        fp = await connector.fingerprint(_TARGET, operator=_make_operator())

    assert fp.reachable is True
    assert fp.vendor == "keycloak"
    assert fp.product == "keycloak"
    assert fp.version == "26.0.5"
    assert fp.probe_method == "GET /admin/realms/evba"
    assert fp.extras["realm"] == "evba"
    assert fp.extras["enabled"] is True
    assert fp.extras["ssl_required"] == "external"
    assert fp.extras["login_theme"] == "meho"
    assert fp.extras["admin_realm"] == "master"
    assert fp.extras["managed_realm"] == "evba"

    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_version_none_when_serverinfo_unavailable() -> None:
    """A 404 on /admin/serverinfo leaves version=None but keeps reachable=True."""
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 60}
        )
        mock.get("/admin/realms/evba").respond(200, json={"realm": "evba", "enabled": True})
        mock.get("/admin/serverinfo").respond(404)
        fp = await connector.fingerprint(_TARGET, operator=_make_operator())

    assert fp.reachable is True
    assert fp.version is None

    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_transport_error() -> None:
    """A token-endpoint failure yields reachable=False with extras['error']."""
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(503)
        fp = await connector.fingerprint(_TARGET, operator=_make_operator())

    assert fp.reachable is False
    assert "error" in fp.extras
    assert fp.version is None

    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_honours_target_realm_overrides() -> None:
    """extras admin_realm / managed_realm steer the token + realm paths."""
    target = _StubTarget(
        name="keycloak-b",
        host="keycloak-b.test.invalid",
        extras={"admin_realm": "ops", "managed_realm": "tenant-x"},
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-b.test.invalid") as mock:
        token_route = mock.post("/realms/ops/protocol/openid-connect/token").respond(
            200, json={"access_token": _ADMIN_TOKEN, "expires_in": 60}
        )
        realm_route = mock.get("/admin/realms/tenant-x").respond(
            200, json={"realm": "tenant-x", "enabled": True}
        )
        mock.get("/admin/serverinfo").respond(404)
        fp = await connector.fingerprint(target, operator=_make_operator())

    assert token_route.called
    assert realm_route.called
    assert fp.extras["admin_realm"] == "ops"
    assert fp.extras["managed_realm"] == "tenant-x"

    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth-model boundary + fail-closed guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_rejects_unsupported_auth_model() -> None:
    """A non-shared_service_account auth_model raises NotImplementedError."""
    connector = _make_connector()
    target = _StubTarget(
        name="keycloak-peruser",
        host="keycloak-peruser.test.invalid",
        auth_model=AuthModel.PER_USER.value,
    )
    with pytest.raises(NotImplementedError, match="keycloak-peruser"):
        await connector.auth_headers(target, operator=_make_operator())

    await connector.aclose()


@pytest.mark.asyncio
async def test_admin_token_fails_closed_without_operator_jwt() -> None:
    """An empty operator.raw_jwt is rejected before the cache lookup."""
    connector = _make_connector()
    with pytest.raises(VaultCredentialsReadError, match="keycloak-a"):
        await connector.auth_headers(_TARGET, operator=_make_operator(raw_jwt=""))

    await connector.aclose()


@pytest.mark.asyncio
async def test_mint_admin_token_raises_on_missing_access_token() -> None:
    """A 200 token response without access_token raises KeycloakAdminTokenError."""
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"token_type": "Bearer"}
        )
        with pytest.raises(KeycloakAdminTokenError, match="keycloak-a"):
            await connector.auth_headers(_TARGET, operator=_make_operator())

    await connector.aclose()


@pytest.mark.asyncio
async def test_token_error_surfaces_upstream_oauth_error_description() -> None:
    """A 401 with an OAuth2 error body echoes Keycloak's error + error_description.

    The whole point of #1474's second ask: ``returned HTTP 401`` alone can't
    distinguish a bad secret from a client-not-allowed-the-grant from a wrong
    realm. Keycloak's ``{error, error_description}`` (non-secret) is echoed so
    the operator diagnoses it in one look.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            401,
            json={
                "error": "unauthorized_client",
                "error_description": "Invalid client or Invalid client credentials",
            },
        )
        with pytest.raises(KeycloakAdminTokenError) as excinfo:
            await connector.auth_headers(_TARGET, operator=_make_operator())

    message = str(excinfo.value)
    assert "returned HTTP 401" in message
    assert "unauthorized_client" in message
    assert "Invalid client or Invalid client credentials" in message

    await connector.aclose()


@pytest.mark.asyncio
async def test_token_error_without_oauth_body_omits_detail() -> None:
    """A non-OAuth2 error body (e.g. an HTML 502) injects no error fragment.

    The detail helper returns ``""`` when the body is not a JSON object
    carrying an ``error`` key, so the bare status-code message is preserved
    rather than appending parse noise.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            502, html="<html><body>Bad Gateway</body></html>"
        )
        with pytest.raises(KeycloakAdminTokenError) as excinfo:
            await connector.auth_headers(_TARGET, operator=_make_operator())

    message = str(excinfo.value)
    assert "returned HTTP 502" in message
    assert "error=" not in message

    await connector.aclose()


@pytest.mark.asyncio
async def test_token_error_ignores_non_oauth2_json_envelope() -> None:
    """A JSON envelope whose ``error`` is not an RFC 6749 §5.2 code is ignored.

    A gateway/proxy can return ``application/json`` carrying an ``error`` key
    that is *not* an OAuth2 token error; its ``error_description`` is not
    schema-bound to be non-secret. The helper gates on the standard code set,
    so such a body degrades to the bare status message — no fragment, no leak.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://keycloak-a.test.invalid") as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            502,
            json={"error": "bad_gateway", "error_description": "upstream-detail-DO-NOT-ECHO"},
        )
        with pytest.raises(KeycloakAdminTokenError) as excinfo:
            await connector.auth_headers(_TARGET, operator=_make_operator())

    message = str(excinfo.value)
    assert "returned HTTP 502" in message
    assert "error=" not in message
    assert "upstream-detail-DO-NOT-ECHO" not in message

    await connector.aclose()


# ---------------------------------------------------------------------------
# Admin-credential discriminator loader — Vault-payload whitespace stripping
# (the #1474 root cause: a client_secret with a trailing \n)
# ---------------------------------------------------------------------------


@pytest.fixture
def _kc_loader_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars the live Vault read path reads at construction.

    ``load_admin_credentials_from_vault`` → ``load_vault_secret_data`` →
    ``vault_client_for_operator`` eagerly reads ``KEYCLOAK_*`` / ``VAULT_*``
    via ``get_settings``; pin before the cache is built and clear after.
    """
    from meho_backplane.settings import get_settings

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


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kc_loader_settings_env")
async def test_admin_loader_strips_client_secret_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact #1474 bug: a client_secret stored with a trailing \\n.

    The connector reads the field verbatim and would send
    ``client_secret=<32 chars>\\n`` to Keycloak → ``unauthorized_client``.
    The discriminator loader strips it at load time.
    """
    from meho_backplane.connectors.keycloak.session import load_admin_credentials_from_vault
    from tests._vault_fakes import install_fake_client

    install_fake_client(
        monkeypatch,
        secret={"client_id": "  meho-admin\n", "client_secret": "32-char-secret-value\n"},
    )

    creds = await load_admin_credentials_from_vault(_TARGET, _make_operator())

    assert isinstance(creds, KeycloakClientCredentials)
    assert creds.client_id == "meho-admin"
    assert creds.client_secret == "32-char-secret-value"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kc_loader_settings_env")
async def test_admin_loader_strips_password_shape_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The password break-glass shape is stripped the same way."""
    from meho_backplane.connectors.keycloak.session import load_admin_credentials_from_vault
    from tests._vault_fakes import install_fake_client

    install_fake_client(
        monkeypatch,
        secret={"username": "admin\n", "password": "pw\n", "client_id": "custom-cli\n"},
    )

    creds = await load_admin_credentials_from_vault(_TARGET, _make_operator())

    assert isinstance(creds, KeycloakPasswordCredentials)
    assert creds.username == "admin"
    assert creds.password == "pw"
    assert creds.client_id == "custom-cli"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kc_loader_settings_env")
async def test_user_write_password_reader_strips_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vault-sourced user password is stripped before it is set on Keycloak.

    Same artifact, different surface: a trailing ``\\n`` on the password the
    user-write op sources from Vault would otherwise be written into Keycloak
    as part of the password, yielding an account that can never authenticate.
    """
    from meho_backplane.connectors.keycloak.ops_write import _read_password_from_vault
    from tests._vault_fakes import install_fake_client

    install_fake_client(monkeypatch, secret={"password": "hunter2\n"})

    value = await _read_password_from_vault(
        _make_operator(),
        _TARGET,
        {"password_secret_ref": "rdc/keycloak/user-pw"},
    )

    assert value == "hunter2"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kc_loader_settings_env")
async def test_user_write_password_reader_rejects_whitespace_only_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only secret is rejected post-strip, not set as empty."""
    from meho_backplane.connectors.keycloak.ops_write import (
        KeycloakPasswordSecretError,
        _read_password_from_vault,
    )
    from tests._vault_fakes import install_fake_client

    install_fake_client(monkeypatch, secret={"password": "   \n"})

    with pytest.raises(KeycloakPasswordSecretError):
        await _read_password_from_vault(
            _make_operator(),
            _TARGET,
            {"password_secret_ref": "rdc/keycloak/user-pw"},
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kc_loader_settings_env")
async def test_user_write_password_reader_honours_custom_key_and_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schemeless ref reads the Vault backend, honouring key + mount (AC #2).

    Regression pin: ``password_secret_key`` selects a non-default field and
    ``password_secret_mount`` is threaded through to the KV-v2 read exactly
    as before the seam routing — a schemeless ref must behave byte-for-byte.
    """
    from meho_backplane.connectors.keycloak.ops_write import _read_password_from_vault
    from tests._vault_fakes import install_fake_client

    fake = install_fake_client(monkeypatch, secret={"db_pw": "s3cret\n"})

    value = await _read_password_from_vault(
        _make_operator(),
        _TARGET,
        {
            "password_secret_ref": "rdc/keycloak/user-pw",
            "password_secret_key": "db_pw",
            "password_secret_mount": "kv2",
        },
    )

    assert value == "s3cret"
    # The Vault backend received the op's mount + path verbatim (mount is a
    # Vault-only knob) — proving schemeless routing is unchanged.
    assert fake.secrets.kv.v2.read_calls[-1] == {
        "path": "rdc/keycloak/user-pw",
        "mount_point": "kv2",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("_kc_loader_settings_env")
async def test_user_write_password_reader_dispatches_gsm_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``gsm:``-schemed ref dispatches to the gsm backend, fragment-selected (AC #1).

    Proves the write-op password read rides the #2229 credential-backend
    seam: an explicit ``gsm:`` scheme reaches a registered gsm backend
    (here a fake), the ``#field`` fragment subsumes ``password_secret_key``,
    and ``password_secret_mount`` (a Vault-only knob) is ignored by gsm.
    """
    from meho_backplane.connectors._shared.credential_backend import (
        CREDENTIAL_BACKEND_REGISTRY,
    )
    from meho_backplane.connectors.keycloak.ops_write import _read_password_from_vault

    seen: dict[str, Any] = {}

    class _FakeGsmBackend:
        async def load_secret_data(
            self,
            secret_ref: str,
            operator: Operator,
            *,
            target_name: str,
            mount: str,
        ) -> dict[str, object]:
            seen["secret_ref"] = secret_ref
            seen["mount"] = mount
            # Mirror gsm's fragment contract: '<path>#<field>' narrows the
            # payload to that one field. The scheme was already split off by
            # the seam, so secret_ref is the scheme-stripped remainder.
            _path, _sep, field = secret_ref.rpartition("#")
            return {field: "gsm-sourced-pw\n"}

    saved = CREDENTIAL_BACKEND_REGISTRY.get("gsm")
    CREDENTIAL_BACKEND_REGISTRY["gsm"] = _FakeGsmBackend()  # type: ignore[assignment]
    try:
        value = await _read_password_from_vault(
            _make_operator(),
            _TARGET,
            {
                "password_secret_ref": "gsm:meho-prod/keycloak-user#password",
                # A Vault-only knob gsm ignores — set to prove it is inert.
                "password_secret_mount": "kv2",
            },
        )
    finally:
        if saved is not None:
            CREDENTIAL_BACKEND_REGISTRY["gsm"] = saved
        else:  # pragma: no cover - gsm self-registers at import
            CREDENTIAL_BACKEND_REGISTRY.pop("gsm", None)

    assert value == "gsm-sourced-pw"
    # Scheme split off, fragment preserved; mount threaded but gsm-inert.
    assert seen["secret_ref"] == "meho-prod/keycloak-user#password"
    assert seen["mount"] == "kv2"


def test_ops_write_has_no_direct_vault_client_usage() -> None:
    """No direct ``vault_client_for_operator`` / hvac read remains (AC #3).

    Grep-pins the seam adoption: the write-op password reader must resolve
    ``password_secret_ref`` through ``load_vault_secret_data``, never open
    an hvac client itself. A regression that re-introduces a direct Vault
    read (the pre-#2401 shape that broke gsm deploys) fails here.
    """
    from pathlib import Path

    import meho_backplane.connectors.keycloak.ops_write as ops_write_module

    source = Path(ops_write_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("vault_client_for_operator", "read_secret_version", "import hvac"):
        assert forbidden not in source, (
            f"keycloak/ops_write.py must not use {forbidden!r} directly — "
            "route the password read through the credential-backend seam "
            "(load_vault_secret_data)"
        )


# ---------------------------------------------------------------------------
# Registrar seam -- T2 (#1394) fills the read-op walk
# ---------------------------------------------------------------------------


def test_read_ops_handler_attrs_resolve_to_bound_methods() -> None:
    """Every READ_OPS ``handler_attr`` resolves to a method on the connector.

    Pins the registration walk's precondition (the ``getattr`` lookup in
    :meth:`KeycloakConnector.register_operations`) without a DB round-trip
    — the DB-backed upsert + dispatch is covered by the E2E suite.
    """
    from meho_backplane.connectors.keycloak.ops_read import READ_OPS, WHEN_TO_USE_BY_GROUP

    assert len(READ_OPS) == 6
    for op in READ_OPS:
        handler = getattr(KeycloakConnector, op.handler_attr, None)
        assert callable(handler), (
            f"{op.op_id} handler_attr={op.handler_attr!r} is not a method on the connector"
        )
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        # Every grouped op has a curated when_to_use (registration asserts this).
        assert op.group_key is not None and op.group_key in WHEN_TO_USE_BY_GROUP
