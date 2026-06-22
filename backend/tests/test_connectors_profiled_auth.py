# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the hoisted profile-driven auth harness (G0.28-T4 #1970).

Pins the four acceptance criteria:

* The per-target lock / token cache / single-flight / re-login-once /
  empty-jwt fail-closed harness lives once in ``ProfiledRestConnector`` and
  is exercised through the named auth scheme.
* vRLI (``session_login``) + keycloak (``oauth2_mint``) auth each reduce to
  a named member with no behaviour loss — token mint + refresh-on-expiry
  parity tests for both.
* ``per_user`` / impersonation is rejected with the standard
  :exc:`NotImplementedError` pattern.
* The empty-``raw_jwt`` fail-closed invariant has an explicit security test.

The stateless schemes (``basic`` / ``static_header``) are covered too — they
prove the catalog's no-session members produce the right header without a
login round-trip.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import ProfileAuthError
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.schemas import AuthModel


def _operator(raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None = 443
    secret_ref: str | None = "p/secret"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _profile(scheme: str, **auth_overrides: object) -> ExecutionProfile:
    """Build a profile for *scheme* with sensible per-scheme defaults."""
    defaults: dict[str, object] = {
        "basic": {"secret_fields": ("username", "password")},
        "static_header": {"secret_fields": ("token",), "value_kind": "bearer"},
        "session_login": {"secret_fields": ("username", "password")},
        "session_login_basic": {"secret_fields": ("username", "password")},
        "oauth2_mint": {"secret_fields": ("client_id", "client_secret")},
    }[scheme]
    auth_kwargs: dict[str, object] = {"scheme": scheme, **defaults}
    auth_kwargs.update(auth_overrides)
    return ExecutionProfile(
        product="acme",
        version="1.0",
        auth=AuthSpec(**auth_kwargs),
        fingerprint=FingerprintSpec(
            path="/api/version", version_key="version", version_splitter="none"
        ),
        probe="delegate",
        pagination=PaginationSpec(strategy="none", items_key="value"),
    )


def _connector(
    scheme: str,
    secret: dict[str, str],
    *,
    profile: ExecutionProfile | None = None,
) -> ProfiledRestConnector:
    """Build a profiled connector with a stub loader returning *secret*."""

    async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
        return dict(secret)

    return ProfiledRestConnector(
        profile=profile if profile is not None else _profile(scheme),
        credentials_loader=_loader,
    )


# ---------------------------------------------------------------------------
# Stateless schemes — basic / static_header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_scheme_builds_basic_auth_header_without_login() -> None:
    """``basic`` computes Authorization: Basic <b64> from the secret bundle."""
    connector = _connector("basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="t", host="t.invalid")

    headers = await connector.auth_headers(target, operator=_operator())

    expected = base64.b64encode(b"svc:pw").decode()
    assert headers == {"Authorization": f"Basic {expected}"}
    # No session token is cached for a stateless scheme.
    assert connector._session_tokens == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_static_header_bearer_wraps_token() -> None:
    """``static_header`` with value_kind=bearer wraps the token as Bearer."""
    connector = _connector("static_header", {"token": "pre-issued-xyz"})
    target = _StubTarget(name="t", host="t.invalid")

    headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"Authorization": "Bearer pre-issued-xyz"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_static_header_raw_places_token_verbatim_in_custom_header() -> None:
    """``static_header`` value_kind=raw places the token verbatim in a custom header."""
    profile = _profile(
        "static_header",
        secret_fields=("token",),
        value_kind="raw",
        header_name="X-Api-Key",
    )
    connector = _connector("static_header", {"token": "raw-key"}, profile=profile)
    target = _StubTarget(name="t", host="t.invalid")

    headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"X-Api-Key": "raw-key"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# session_login (vRLI parity) — mint + cache + single-flight + refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_login_mints_token_via_json_post_and_returns_bearer() -> None:
    """``session_login`` POSTs a JSON login body and returns Bearer <sessionId>.

    Behaviour parity with VcfLogsConnector: JSON body, ``sessionId`` read
    out of the response body, no Authorization header on the login POST.
    """
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    async with respx.mock(base_url="https://vrli.invalid") as mock:
        route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": "sess-abc", "ttl": 1800}
        )
        headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"Authorization": "Bearer sess-abc"}
    request = route.calls[0].request
    assert request.headers.get("content-type", "").startswith("application/json")
    import json

    assert json.loads(request.read().decode()) == {
        "username": "svc",
        "password": "pw",
        "provider": "Local",
    }
    # The login POST must NOT carry a stale Authorization header.
    assert "authorization" not in {k.lower() for k in request.headers}
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_caches_token_single_flight() -> None:
    """A second call reuses the cached session — exactly one login POST."""
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    async with respx.mock(base_url="https://vrli.invalid") as mock:
        route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": "sess-cached", "ttl": 1800}
        )
        h1 = await connector.auth_headers(target, operator=_operator())
        h2 = await connector.auth_headers(target, operator=_operator())

    assert h1 == h2 == {"Authorization": "Bearer sess-cached"}
    assert route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_does_not_proactively_expire_on_ttl() -> None:
    """A session_login token has no proactive TTL — it caches until invalidated.

    vRLI's session is idle-expiry-driven, recovered by a downstream
    re-login. The cache entry must therefore carry ``expires_at=None`` so a
    long-lived connector keeps reusing the token rather than re-minting on a
    clock-based margin (behaviour parity with the typed connector).
    """
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    async with respx.mock(base_url="https://vrli.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "s", "ttl": 5})
        await connector.auth_headers(target, operator=_operator())

    cached = connector._session_tokens[target_cache_key(target)]
    assert cached.expires_at is None
    assert cached.is_fresh(time.monotonic() + 10_000) is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_missing_token_raises_naming_scheme_and_target() -> None:
    """A 2xx login with no sessionId raises ProfileAuthError naming the target."""
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    async with respx.mock(base_url="https://vrli.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"ttl": 1800})
        with pytest.raises(ProfileAuthError, match=r"vrli") as exc:
            await connector.auth_headers(target, operator=_operator())

    assert "session_login" in str(exc.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# session_login_basic (vCenter parity) — basic-auth login -> raw-string token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_login_basic_mints_token_via_basic_auth_post() -> None:
    """``session_login_basic`` POSTs with HTTP Basic and no body.

    Behaviour parity with VmwareRestConnector's modern ``/api/session``:
    credentials ride an HTTP Basic ``Authorization`` header (not the body),
    the response body *is* the raw JSON-string token, and the established
    token is applied verbatim in the ``vmware-api-session-id`` header.
    """
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        route = mock.post("/api/session").respond(200, json="sess-token-xyz")
        headers = await connector.auth_headers(target, operator=_operator())

    # Raw token (no Bearer wrap) in vCenter's bespoke session header.
    assert headers == {"vmware-api-session-id": "sess-token-xyz"}
    request = route.calls[0].request
    # Credentials ride HTTP Basic, not the request body.
    expected_basic = base64.b64encode(b"svc:pw").decode()
    assert request.headers.get("authorization") == f"Basic {expected_basic}"
    # The login POST carries an empty body (no JSON creds).
    assert request.read() == b""
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_basic_caches_token_single_flight() -> None:
    """A second call reuses the cached vCenter session — exactly one login POST."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        route = mock.post("/api/session").respond(200, json="sess-cached")
        h1 = await connector.auth_headers(target, operator=_operator())
        h2 = await connector.auth_headers(target, operator=_operator())

    assert h1 == h2 == {"vmware-api-session-id": "sess-cached"}
    assert route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_basic_does_not_proactively_expire_on_ttl() -> None:
    """A session_login_basic token has no proactive TTL — caches until invalidated."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        mock.post("/api/session").respond(200, json="s")
        await connector.auth_headers(target, operator=_operator())

    cached = connector._session_tokens[target_cache_key(target)]
    assert cached.expires_at is None
    assert cached.is_fresh(time.monotonic() + 10_000) is True
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_basic_mints_token_from_legacy_object_body() -> None:
    """The legacy ``{"value": "<tok>"}`` object body mints the token (#2047).

    Pre-7.0 vCenter (and some vcsim builds) — the endpoint #2031's 404
    login-path fallback reaches — return the token nested under ``value``
    rather than as a raw JSON string. The profiled extractor now reads that
    shape, mirroring the typed connector, so the legacy fallback can
    actually establish a session.
    """
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        mock.post("/api/session").respond(200, json={"value": "legacy-tok"})
        headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"vmware-api-session-id": "legacy-tok"}
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, {"value": ""}, {"value": 123}, [], "", 123],
)
async def test_session_login_basic_unusable_body_raises_naming_target(
    payload: object,
) -> None:
    """A 2xx login with no usable token raises naming the target.

    A non-string / non-``{"value": <non-empty str>}`` body — empty object,
    empty/non-string ``value``, list, empty string, bare non-string — is
    treated as "no usable token" so the harness raises the consistent
    target-named :exc:`ProfileAuthError`.
    """
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        mock.post("/api/session").respond(200, json=payload)
        with pytest.raises(ProfileAuthError, match=r"vcenter") as exc:
            await connector.auth_headers(target, operator=_operator())

    assert "session_login_basic" in str(exc.value)
    await connector.aclose()


def test_session_login_basic_build_body_and_extract_token_directly() -> None:
    """Unit-level cover of the scheme's build_body + extract_token + login auth.

    Mirrors the ``session_login`` direct-spec coverage: ``build_body``
    returns an empty body (creds ride HTTP Basic), ``build_login_auth``
    yields the ``(username, password)`` pair, and ``extract_token`` coerces
    both the modern raw JSON-string body and the legacy
    ``{"value": "<tok>"}`` object body to a no-TTL token while rejecting
    non-string / empty / malformed bodies.
    """
    from meho_backplane.connectors._shared.profile_auth import (
        SESSION_SCHEME_SPECS,
        SESSION_TOKEN_OBJECT_KEY,
    )

    spec = SESSION_SCHEME_SPECS["session_login_basic"]
    auth = AuthSpec(scheme="session_login_basic", secret_fields=("username", "password"))
    secret = {"username": "svc", "password": "pw"}

    assert spec.build_body(auth, secret) == {}
    assert spec.build_login_auth(auth, secret) == ("svc", "pw")
    assert spec.login_path(auth) == "/api/session"
    assert spec.login_credentials == "basic"
    assert spec.token_header == "vmware-api-session-id"
    assert spec.token_value_kind == "raw"

    # Modern: raw JSON-string body is the token.
    minted = spec.extract_token("raw-string-token")
    assert minted is not None
    assert minted.token == "raw-string-token"
    assert minted.ttl_seconds is None

    # Legacy (#2047): the token nested under the shared object key.
    legacy = spec.extract_token({SESSION_TOKEN_OBJECT_KEY: "obj-tok"})
    assert legacy is not None
    assert legacy.token == "obj-tok"
    assert legacy.ttl_seconds is None

    # No usable token → None (harness then raises the target-named error).
    assert spec.extract_token("") is None
    assert spec.extract_token({}) is None
    assert spec.extract_token({SESSION_TOKEN_OBJECT_KEY: ""}) is None
    assert spec.extract_token({SESSION_TOKEN_OBJECT_KEY: 123}) is None
    assert spec.extract_token([]) is None
    assert spec.extract_token(123) is None


# ---------------------------------------------------------------------------
# session_login_basic modern→legacy 404 fallback + op-path mount (#2031)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_login_basic_uses_modern_path_when_served() -> None:
    """The modern ``/api/session`` wins when served; no legacy attempt."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid", assert_all_called=False) as mock:
        modern = mock.post("/api/session").respond(200, json="modern-tok")
        legacy = mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-tok")
        headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"vmware-api-session-id": "modern-tok"}
    assert modern.call_count == 1
    assert legacy.call_count == 0
    assert connector._session_login_paths[target_cache_key(target)] == "/api/session"
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_basic_falls_back_to_legacy_on_404() -> None:
    """A 404 on modern triggers exactly one legacy ``/rest/...`` retry."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcsim", host="vcsim.invalid")

    async with respx.mock(base_url="https://vcsim.invalid") as mock:
        modern = mock.post("/api/session").respond(404)
        legacy = mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-tok")
        headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"vmware-api-session-id": "legacy-tok"}
    assert modern.call_count == 1
    assert legacy.call_count == 1
    # Both attempts carry the same HTTP Basic credentials, no body.
    expected_basic = base64.b64encode(b"svc:pw").decode()
    assert legacy.calls[0].request.headers.get("authorization") == f"Basic {expected_basic}"
    assert legacy.calls[0].request.read() == b""
    assert connector._session_login_paths[target_cache_key(target)] == (
        "/rest/com/vmware/cis/session"
    )
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("modern_status", [401, 403, 500, 503])
async def test_session_login_basic_does_not_fall_back_on_non_404(modern_status: int) -> None:
    """401 / 403 / 5xx on modern are auth/server failures — no legacy retry."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid", assert_all_called=False) as mock:
        modern = mock.post("/api/session").respond(modern_status)
        legacy = mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-tok")
        with pytest.raises(httpx.HTTPStatusError):
            await connector.auth_headers(target, operator=_operator())

    assert modern.call_count == 1
    assert legacy.call_count == 0
    # No session recorded — the establish failed.
    assert target_cache_key(target) not in connector._session_login_paths
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_login_basic_legacy_404_too_surfaces_status_error() -> None:
    """When both modern and legacy 404, the error surfaces (no third attempt)."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        modern = mock.post("/api/session").respond(404)
        legacy = mock.post("/rest/com/vmware/cis/session").respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            await connector.auth_headers(target, operator=_operator())

    assert modern.call_count == 1
    assert legacy.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_mount_op_path_modern_prefixes_api() -> None:
    """When modern wins, a spec-relative op path mounts under ``/api``."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcenter", host="vcenter.invalid")

    async with respx.mock(base_url="https://vcenter.invalid") as mock:
        mock.post("/api/session").respond(200, json="modern-tok")
        mounted = await connector.mount_op_path(target, "/vcenter/vm", operator=_operator())

    assert mounted == "/api/vcenter/vm"
    await connector.aclose()


@pytest.mark.asyncio
async def test_mount_op_path_legacy_prefixes_rest() -> None:
    """When legacy wins (vcsim), a spec-relative op path mounts under ``/rest``."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcsim", host="vcsim.invalid")

    async with respx.mock(base_url="https://vcsim.invalid") as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-tok")
        mounted = await connector.mount_op_path(target, "/vcenter/vm", operator=_operator())

    assert mounted == "/rest/vcenter/vm"
    await connector.aclose()


@pytest.mark.asyncio
async def test_mount_op_path_passes_through_already_mounted_path() -> None:
    """A descriptor path already carrying a known mount prefix is unchanged."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcsim", host="vcsim.invalid")

    async with respx.mock(base_url="https://vcsim.invalid") as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-tok")
        # Legacy won, but an explicit /api/... descriptor isn't re-prefixed.
        mounted = await connector.mount_op_path(target, "/api/vcenter/vm", operator=_operator())

    assert mounted == "/api/vcenter/vm"
    await connector.aclose()


@pytest.mark.asyncio
async def test_mount_op_path_identity_for_scheme_without_fallback() -> None:
    """A no-fallback session scheme (vRLI) returns the path unchanged."""
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    # No login round-trip needed — a scheme without a fallback is identity.
    mounted = await connector.mount_op_path(target, "/api/v2/events", operator=_operator())

    assert mounted == "/api/v2/events"
    # No session was established for the identity path.
    assert connector._session_login_paths == {}
    await connector.aclose()


@pytest.mark.asyncio
async def test_mount_op_path_identity_for_stateless_scheme() -> None:
    """A stateless scheme (basic) needs no session establish to mount."""
    connector = _connector("basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="t", host="t.invalid")

    mounted = await connector.mount_op_path(target, "/api/v2.0/projects", operator=_operator())

    assert mounted == "/api/v2.0/projects"
    await connector.aclose()


@pytest.mark.asyncio
async def test_invalidate_session_drops_recorded_login_path() -> None:
    """Invalidating a session clears its recorded login path too (#2031)."""
    connector = _connector("session_login_basic", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vcsim", host="vcsim.invalid")

    async with respx.mock(base_url="https://vcsim.invalid") as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json="legacy-tok")
        await connector.auth_headers(target, operator=_operator())

    assert target_cache_key(target) in connector._session_login_paths
    await connector._invalidate_session(target)
    assert target_cache_key(target) not in connector._session_login_paths
    await connector.aclose()


def test_session_login_basic_declares_vetted_legacy_fallback() -> None:
    """The fallback is a closed, vetted vCenter pair — and the only one."""
    from meho_backplane.connectors._shared.profile_auth import SESSION_SCHEME_SPECS

    fallback = SESSION_SCHEME_SPECS["session_login_basic"].legacy_fallback
    assert fallback is not None
    assert fallback.legacy_login_path == "/rest/com/vmware/cis/session"
    assert fallback.modern_op_mount == "/api"
    assert fallback.legacy_op_mount == "/rest"
    assert fallback.op_mount_for_login_path("/api/session") == "/api"
    assert fallback.op_mount_for_login_path("/rest/com/vmware/cis/session") == "/rest"
    # No other scheme carries a fallback — vRLI / keycloak are single-path.
    assert SESSION_SCHEME_SPECS["session_login"].legacy_fallback is None
    assert SESSION_SCHEME_SPECS["oauth2_mint"].legacy_fallback is None


# ---------------------------------------------------------------------------
# oauth2_mint (keycloak parity) — form grant + TTL refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth2_mint_uses_form_client_credentials_grant() -> None:
    """``oauth2_mint`` POSTs a form-encoded client-credentials grant.

    Behaviour parity with KeycloakConnector: form body (NOT JSON),
    grant_type=client_credentials, client_id/client_secret from the secret
    bundle, Bearer access_token returned.
    """
    connector = _connector("oauth2_mint", {"client_id": "cid", "client_secret": "csec"})
    target = _StubTarget(name="kc", host="kc.invalid")

    async with respx.mock(base_url="https://kc.invalid") as mock:
        route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "tok-kc", "expires_in": 300}
        )
        headers = await connector.auth_headers(target, operator=_operator())

    assert headers == {"Authorization": "Bearer tok-kc"}
    request = route.calls[0].request
    assert request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
    # Form-encoded body carries the grant fields.
    body = request.read().decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "client_secret=csec" in body
    await connector.aclose()


@pytest.mark.asyncio
async def test_oauth2_mint_refreshes_on_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oauth2_mint token re-mints once the monotonic clock passes its TTL.

    The refresh-on-expiry half of the behaviour-parity criterion: with a
    short ``expires_in``, advancing the monotonic clock past the
    margin-adjusted TTL forces a second token mint rather than serving the
    stale token.
    """
    connector = _connector("oauth2_mint", {"client_id": "cid", "client_secret": "csec"})
    target = _StubTarget(name="kc", host="kc.invalid")

    clock = {"t": 1000.0}
    monkeypatch.setattr("meho_backplane.connectors.profiled.time.monotonic", lambda: clock["t"])

    async with respx.mock(base_url="https://kc.invalid") as mock:
        route = mock.post("/realms/master/protocol/openid-connect/token")
        route.side_effect = [
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 60}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": 60}),
        ]
        h1 = await connector.auth_headers(target, operator=_operator())
        # expires_in=60 minus 30s margin => fresh for 30s; advance past it.
        clock["t"] += 31.0
        h2 = await connector.auth_headers(target, operator=_operator())

    assert h1 == {"Authorization": "Bearer tok-1"}
    assert h2 == {"Authorization": "Bearer tok-2"}
    assert route.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_oauth2_mint_caches_within_ttl() -> None:
    """Within the TTL window the token is reused — one mint for two calls."""
    connector = _connector("oauth2_mint", {"client_id": "cid", "client_secret": "csec"})
    target = _StubTarget(name="kc", host="kc.invalid")

    async with respx.mock(base_url="https://kc.invalid") as mock:
        route = mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "tok", "expires_in": 3600}
        )
        await connector.auth_headers(target, operator=_operator())
        await connector.auth_headers(target, operator=_operator())

    assert route.call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-target isolation — the hoisted cache keys on (tenant_id, id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_tokens_separate() -> None:
    """Two targets get two distinct cached tokens; no cross-target leakage."""
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target_a = _StubTarget(name="a", host="a.invalid")
    target_b = _StubTarget(name="b", host="b.invalid")

    async with respx.mock() as mock:
        mock.post("https://a.invalid/api/v2/sessions").respond(200, json={"sessionId": "tok-a"})
        mock.post("https://b.invalid/api/v2/sessions").respond(200, json={"sessionId": "tok-b"})
        h_a = await connector.auth_headers(target_a, operator=_operator())
        h_b = await connector.auth_headers(target_b, operator=_operator())

    assert h_a == {"Authorization": "Bearer tok-a"}
    assert h_b == {"Authorization": "Bearer tok-b"}
    assert connector._session_tokens[target_cache_key(target_a)].token == "tok-a"
    assert connector._session_tokens[target_cache_key(target_b)].token == "tok-b"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth-model gating — per_user / impersonation rejected (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """per_user / impersonation raise NotImplementedError naming target + mode."""
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="t-pu", host="t.invalid", auth_model=auth_model)

    with pytest.raises(NotImplementedError) as exc:
        await connector.auth_headers(target, operator=_operator())

    assert "t-pu" in str(exc.value)
    assert auth_model in str(exc.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3) is accepted (basic scheme, no login)."""
    connector = _connector("basic", {"username": "u", "password": "p"})
    target = _StubTarget(name="t", host="t.invalid", auth_model=None)

    headers = await connector.auth_headers(target, operator=_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# Empty-jwt fail-closed invariant (AC4) — explicit security test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_jwt_fails_closed_before_session_cache_lookup() -> None:
    """A system-initiated caller (empty raw_jwt) is rejected before the cache.

    Security-load-bearing invariant: a token primed by an authenticated
    caller must never be served to an empty-JWT caller via a cache hit. The
    guard runs before the cache lookup, so even with a warm cache the
    empty-JWT call fails closed.
    """
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    # Prime the cache with an authenticated caller.
    async with respx.mock(base_url="https://vrli.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "warm"})
        await connector.auth_headers(target, operator=_operator())
    assert target_cache_key(target) in connector._session_tokens

    # An empty-JWT caller must NOT be served the warm token.
    with pytest.raises(VaultCredentialsReadError, match=r"vrli"):
        await connector.auth_headers(target, operator=_operator(raw_jwt=""))
    await connector.aclose()


@pytest.mark.asyncio
async def test_empty_jwt_does_not_leak_token_to_unauthenticated_caller() -> None:
    """The empty-JWT rejection returns no token even when the cache is warm."""
    connector = _connector("oauth2_mint", {"client_id": "c", "client_secret": "s"})
    target = _StubTarget(name="kc", host="kc.invalid")

    with pytest.raises(VaultCredentialsReadError):
        await connector.auth_headers(target, operator=_operator(raw_jwt=""))
    # No mint happened, no token cached.
    assert connector._session_tokens == {}
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose + no-profile guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_session_cache_and_pool() -> None:
    """aclose() clears the session-token cache and tears down the httpx pool."""
    connector = _connector("session_login", {"username": "svc", "password": "pw"})
    target = _StubTarget(name="vrli", host="vrli.invalid")

    async with respx.mock(base_url="https://vrli.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "tok"})
        await connector.auth_headers(target, operator=_operator())

    assert connector._session_tokens != {}
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_no_profile_attached_raises_naming_target() -> None:
    """A profiled connector with no profile raises a clear NotImplementedError."""
    connector = ProfiledRestConnector()
    target = _StubTarget(name="orphan", host="t.invalid")

    with pytest.raises(NotImplementedError, match=r"orphan"):
        await connector.auth_headers(target, operator=_operator())
    await connector.aclose()


def test_class_attribute_profile_is_used_when_not_injected() -> None:
    """A stamped subclass carrying a class-level profile is used at construction."""

    class _StampedProfiled(ProfiledRestConnector):
        product = "acme"
        supported_version_range = ">=1.0,<2.0"
        profile = _profile("basic")

    instance = _StampedProfiled()
    assert instance.profile is _StampedProfiled.profile
    assert instance.profile is not None
    assert instance.profile.auth.scheme == "basic"
