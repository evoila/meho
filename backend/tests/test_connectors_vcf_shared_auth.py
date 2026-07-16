# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.connectors._shared.vcf_auth` (G3.6-T13 #841).

Covers the four reusable helpers per the issue's acceptance criteria:

1. ``basic_auth_header`` — header value shape.
2. ``is_acceptable_auth_model`` — accept ``SHARED_SERVICE_ACCOUNT`` (enum +
   string) + ``None``; reject ``per_user`` / ``impersonation`` / unknown.
3. ``CredentialsCache`` — load-once, cache-per-target, missing-key →
   ``RuntimeError`` naming target.
4. ``vcf_session_login`` — POST credentials, return token, raise
   :exc:`SessionLoginError` on 401 or empty-token response.

The downstream-call 401-retry-once loop is NOT tested here — that lives in
the consuming connector module (vRLI #830 owns its retry shape). This file
covers only the *login round-trip* this module is responsible for.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    CredentialsCache,
    SessionLoginError,
    VcfTargetLike,
    basic_auth_header,
    is_acceptable_auth_model,
    load_credentials_from_vault,
    vcf_session_login,
)
from meho_backplane.connectors.schemas import AuthModel

# ---------------------------------------------------------------------------
# Target stub — satisfies VcfTargetLike Protocol structurally
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=lambda: UUID(int=1))
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


_TARGET_A = _StubTarget(
    name="vrops-a",
    host="vrops-a.test.invalid",
    port=443,
    secret_ref="vcf-operations/vrops-a",
    id=UUID(int=0xA),
    tenant_id=UUID(int=0),
)
_TARGET_B = _StubTarget(
    name="vrops-b",
    host="vrops-b.test.invalid",
    port=443,
    secret_ref="vcf-operations/vrops-b",
    id=UUID(int=0xB),
    tenant_id=UUID(int=0),
)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode ``Authorization: Basic <b64>`` into ``(username, password)``."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# basic_auth_header
# ---------------------------------------------------------------------------


def test_basic_auth_header_encodes_username_and_password() -> None:
    header = basic_auth_header("admin", "stub-password")
    assert header.startswith("Basic ")
    username, password = _decode_basic_auth(header)
    assert username == "admin"
    assert password == "stub-password"


def test_basic_auth_header_handles_non_ascii_password() -> None:
    """Vault may legitimately return passwords with UTF-8 codepoints."""
    header = basic_auth_header("admin", "pässwörd-€")
    username, password = _decode_basic_auth(header)
    assert username == "admin"
    assert password == "pässwörd-€"


def test_basic_auth_header_preserves_special_username_forms() -> None:
    """Some vendor account forms include `$`, `@`, `\\` — passed verbatim.

    No vendor in the four VCF products uses these forms today, but the
    helper has no business mangling whatever Vault returns.
    """
    header = basic_auth_header("svc\\admin@local", "p")
    username, _ = _decode_basic_auth(header)
    assert username == "svc\\admin@local"


# ---------------------------------------------------------------------------
# is_acceptable_auth_model
# ---------------------------------------------------------------------------


def test_is_acceptable_auth_model_accepts_enum_member() -> None:
    assert is_acceptable_auth_model(AuthModel.SHARED_SERVICE_ACCOUNT) is True


def test_is_acceptable_auth_model_accepts_string_value() -> None:
    assert is_acceptable_auth_model("shared_service_account") is True


def test_is_acceptable_auth_model_accepts_none_for_pre_g03_targets() -> None:
    """The auth_model column is nullable for pre-G0.3 targets — treat as default."""
    assert is_acceptable_auth_model(None) is True


@pytest.mark.parametrize(
    "value",
    [
        AuthModel.PER_USER,
        AuthModel.PER_USER.value,
        AuthModel.IMPERSONATION,
        AuthModel.IMPERSONATION.value,
        "unknown-mode",
        "",
        0,
        False,
    ],
)
def test_is_acceptable_auth_model_rejects_everything_else(value: object) -> None:
    assert is_acceptable_auth_model(value) is False


# ---------------------------------------------------------------------------
# load_credentials_from_vault — live operator-context Vault read (G3.10-T2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_credentials_loader_reads_under_operator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default loader delegates to the shared operator-context KV-v2 read.

    Patches ``meho_backplane.auth.vault._build_client`` with the
    in-process fake so the real
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper runs verbatim against canned creds. Asserts the operator's
    JWT was forwarded to the JWT/OIDC auth method and the read targeted
    the secret_ref path.
    """
    from meho_backplane.settings import get_settings
    from tests._vault_fakes import install_fake_client

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()

    fake = install_fake_client(
        monkeypatch,
        secret={"username": "svc-shared-vcf", "password": "shared-vcf-pw"},
    )
    try:
        operator = _make_operator(raw_jwt="op.shared.vcf.jwt")
        creds = await load_credentials_from_vault(_TARGET_A, operator)

        assert creds == {"username": "svc-shared-vcf", "password": "shared-vcf-pw"}
        # JWT forwarded to Vault's JWT/OIDC auth method.
        assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.shared.vcf.jwt"
        # Read targeted the stripped secret_ref path.
        assert fake.secrets.kv.v2.read_calls[-1]["path"] == _TARGET_A.secret_ref
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_default_credentials_loader_rejects_empty_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``raw_jwt`` is fail-closed — no silent fallback to a system role."""
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()

    try:
        operator = _make_operator(raw_jwt="")
        with pytest.raises(VaultCredentialsReadError) as exc_info:
            await load_credentials_from_vault(_TARGET_A, operator)
        assert "vrops-a" in str(exc_info.value)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# CredentialsCache — happy path, caching, isolation
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin", "password": "stub-password"}


@pytest.mark.asyncio
async def test_credentials_cache_loads_and_returns_keys() -> None:
    cache = CredentialsCache(_stub_loader, product_label="vrops")
    creds = await cache.get(_TARGET_A, _make_operator())
    assert creds == {"username": "admin", "password": "stub-password"}


@pytest.mark.asyncio
async def test_credentials_cache_reuses_value_across_calls() -> None:
    """Second get() against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin", "password": "stub-password"}

    cache = CredentialsCache(_counting_loader, product_label="vrops")
    a = await cache.get(_TARGET_A, _make_operator())
    b = await cache.get(_TARGET_A, _make_operator())
    assert a == b
    assert call_count == 1


@pytest.mark.asyncio
async def test_credentials_cache_threads_operator_to_loader() -> None:
    """The operator passed to ``get`` reaches the loader on first call.

    Load-bearing for the operator-context Vault read: a stale or
    missing operator would silently authenticate as the wrong identity.
    """
    captured: list[Operator] = []

    async def _capture_loader(_target: VcfTargetLike, operator: Operator) -> dict[str, str]:
        captured.append(operator)
        return {"username": "admin", "password": "p"}

    cache = CredentialsCache(_capture_loader, product_label="vrops")
    operator = _make_operator(raw_jwt="captured.jwt")
    await cache.get(_TARGET_A, operator)
    assert captured == [operator]
    assert captured[0].raw_jwt == "captured.jwt"


@pytest.mark.asyncio
async def test_credentials_cache_isolates_per_target() -> None:
    """Two targets get two distinct cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    cache = CredentialsCache(_tracking_loader, product_label="vrli")
    a = await cache.get(_TARGET_A, _make_operator())
    b = await cache.get(_TARGET_B, _make_operator())
    assert a["username"] == "svc-vrops-a"
    assert b["username"] == "svc-vrops-b"
    assert call_log == ["vrops-a", "vrops-b"]
    assert cache.cached_targets == frozenset(
        {target_cache_key(_TARGET_A), target_cache_key(_TARGET_B)}
    )


@pytest.mark.asyncio
async def test_credentials_cache_isolates_same_name_across_tenants() -> None:
    """Two same-named targets in DIFFERENT tenants get distinct cache entries.

    Regression guard for #1642: keying the cache on ``target.name`` alone
    collapsed same-named targets across tenants onto one entry, so one
    tenant could be served another tenant's cached credential. The cache
    is keyed on the tenant-unique ``(tenant_id, id)`` tuple instead.
    """
    load_count = 0

    async def _counting_loader(target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal load_count
        load_count += 1
        # Username encodes the tenant so a cross-tenant cache hit is
        # observable in the returned credential, not just the load count.
        return {"username": f"svc-{target.tenant_id}", "password": "pass"}

    # Same NAME, same id-shape, but two different tenants.
    tenant_one = _StubTarget(
        name="shared-name",
        host="appliance-one.test.invalid",
        port=443,
        secret_ref="vcf/shared-name",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="shared-name",
        host="appliance-two.test.invalid",
        port=443,
        secret_ref="vcf/shared-name",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    cache = CredentialsCache(_counting_loader, product_label="vrops")
    creds_one = await cache.get(tenant_one, _make_operator())
    creds_two = await cache.get(tenant_two, _make_operator())

    # Each tenant triggered its own load — no cross-tenant cache hit.
    assert load_count == 2
    assert creds_one["username"] == f"svc-{tenant_one.tenant_id}"
    assert creds_two["username"] == f"svc-{tenant_two.tenant_id}"
    assert creds_one != creds_two
    assert cache.cached_targets == frozenset(
        {target_cache_key(tenant_one), target_cache_key(tenant_two)}
    )

    # Same-tenant re-fetch is a cache HIT — behaviour unchanged.
    creds_one_again = await cache.get(tenant_one, _make_operator())
    assert load_count == 2
    assert creds_one_again == creds_one


@pytest.mark.asyncio
async def test_credentials_cache_invalidate_drops_only_that_target() -> None:
    cache = CredentialsCache(_stub_loader, product_label="fleet")
    await cache.get(_TARGET_A, _make_operator())
    await cache.get(_TARGET_B, _make_operator())
    await cache.invalidate(_TARGET_A)
    assert cache.cached_targets == frozenset({target_cache_key(_TARGET_B)})


@pytest.mark.asyncio
async def test_credentials_cache_clear_drops_all_entries() -> None:
    cache = CredentialsCache(_stub_loader, product_label="fleet")
    await cache.get(_TARGET_A, _make_operator())
    await cache.get(_TARGET_B, _make_operator())
    await cache.clear()
    assert cache.cached_targets == frozenset()


# ---------------------------------------------------------------------------
# CredentialsCache — missing-key error contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_cache_missing_username_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"password": "stub-password"}

    cache = CredentialsCache(_bad_loader, product_label="vrops")
    with pytest.raises(RuntimeError) as exc_info:
        await cache.get(_TARGET_A, _make_operator())
    msg = str(exc_info.value)
    assert "vrops" in msg
    assert "vrops-a" in msg
    assert "username" in msg


@pytest.mark.asyncio
async def test_credentials_cache_missing_password_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "admin"}

    cache = CredentialsCache(_bad_loader, product_label="vrli")
    with pytest.raises(RuntimeError) as exc_info:
        await cache.get(_TARGET_A, _make_operator())
    msg = str(exc_info.value)
    assert "vrli" in msg
    assert "vrops-a" in msg
    assert "password" in msg


@pytest.mark.asyncio
async def test_credentials_cache_missing_key_does_not_poison_subsequent_attempts() -> None:
    """A first-call missing-key failure must not persist a half-built entry.

    The contract is "load-once, succeed-or-raise". If the loader is fixed
    between calls (e.g. an operator amends a Vault path), the next get()
    must retry.
    """
    attempt = 0

    async def _eventually_correct_loader(
        _target: VcfTargetLike, _operator: Operator
    ) -> dict[str, str]:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return {"username": "admin"}
        return {"username": "admin", "password": "stub-password"}

    cache = CredentialsCache(_eventually_correct_loader, product_label="vrops")
    with pytest.raises(RuntimeError):
        await cache.get(_TARGET_A, _make_operator())
    # Second attempt must succeed; the cache must not have poisoned itself.
    creds = await cache.get(_TARGET_A, _make_operator())
    assert creds == {"username": "admin", "password": "stub-password"}
    assert attempt == 2


# ---------------------------------------------------------------------------
# vcf_session_login — happy paths
# ---------------------------------------------------------------------------


@pytest.fixture
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url="https://vrli-a.test.invalid") as client:
        yield client


@pytest.mark.asyncio
async def test_vcf_session_login_returns_token_from_response_header(
    _client: httpx.AsyncClient,
) -> None:
    """vRLI shape: token is in the ``sessionId`` response header."""
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(
            200,
            json={},
            headers={"sessionId": "header-token-xyz"},
        )
        token = await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            token_extractor=lambda r: r.headers.get("sessionId"),
        )
    assert token == "header-token-xyz"


@pytest.mark.asyncio
async def test_vcf_session_login_returns_token_from_response_body(
    _client: httpx.AsyncClient,
) -> None:
    """A hypothetical product returning the token in the JSON body."""
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "body-token-abc"})
        token = await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            token_extractor=lambda r: r.json().get("sessionId"),
        )
    assert token == "body-token-abc"


@pytest.mark.asyncio
async def test_vcf_session_login_uses_default_payload_when_none_provided(
    _client: httpx.AsyncClient,
) -> None:
    """Default payload is ``{"username": ..., "password": ...}``."""
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, headers={"sessionId": "tok"})

    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").mock(side_effect=_capture)
        await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            token_extractor=lambda r: r.headers.get("sessionId"),
        )

    body = captured["content"]
    assert isinstance(body, bytes)
    parsed = json.loads(body.decode())
    assert parsed == {"username": "admin", "password": "p"}


@pytest.mark.asyncio
async def test_vcf_session_login_payload_builder_takes_precedence(
    _client: httpx.AsyncClient,
) -> None:
    """vRLI shape: builder injects ``"provider"`` field."""
    captured_bodies: list[bytes] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(request.content)
        return httpx.Response(200, headers={"sessionId": "tok"})

    def _vrli_builder(username: str, password: str) -> dict[str, object]:
        return {"username": username, "password": password, "provider": "Local"}

    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").mock(side_effect=_capture)
        await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            payload_builder=_vrli_builder,
            token_extractor=lambda r: r.headers.get("sessionId"),
        )

    parsed = json.loads(captured_bodies[0].decode())
    assert parsed == {
        "username": "admin",
        "password": "p",
        "provider": "Local",
    }


# ---------------------------------------------------------------------------
# vcf_session_login — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vcf_session_login_401_raises_connector_auth_error_naming_target(
    _client: httpx.AsyncClient,
) -> None:
    """A 401 on the login round-trip surfaces the structured :exc:`ConnectorAuthError`.

    #2329: a 401 (rotated/stale password) at establish is an auth-class
    failure, so ``vcf_session_login`` now raises the structured
    ``ConnectorAuthError`` -- a subclass of :exc:`SessionLoginError`, so
    existing ``except SessionLoginError`` callers are unaffected -- carrying
    the status + establish cause sub-code the dispatcher maps to
    ``connector_auth_failed``.

    Note: the *retry-once on 401 around downstream calls* is a separate
    layer the consumer (vRLI #830) owns. The login round-trip itself
    failing 401 means the credentials are wrong — no retry value.
    """
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(401, json={"error": "invalid_credentials"})
        with pytest.raises(SessionLoginError) as exc_info:
            await vcf_session_login(
                _client,
                "/api/v2/sessions",
                username="admin",
                password="wrong",
                target_name="vrli-a",
                token_extractor=lambda r: r.headers.get("sessionId"),
            )
    err = exc_info.value
    assert isinstance(err, ConnectorAuthError)
    assert err.status_code == 401
    assert err.cause == "session_establish_401"
    assert err.target_name == "vrli-a"
    msg = str(err)
    assert "vrli-a" in msg
    assert "401" in msg
    # The cause chain carries the underlying httpx error so callers can
    # introspect status / response if they need to.
    assert isinstance(err.__cause__, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_vcf_session_login_403_raises_connector_auth_error(
    _client: httpx.AsyncClient,
) -> None:
    """A 403 (locked-out / scope-stripped account) also classifies as auth-class (#2329)."""
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(403, json={"error": "account_locked"})
        with pytest.raises(ConnectorAuthError) as exc_info:
            await vcf_session_login(
                _client,
                "/api/v2/sessions",
                username="admin",
                password="p",
                target_name="vrli-a",
                token_extractor=lambda r: r.headers.get("sessionId"),
            )
    assert exc_info.value.cause == "session_establish_403"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_vcf_session_login_500_raises_plain_session_login_error(
    _client: httpx.AsyncClient,
) -> None:
    """A 500 is NOT auth-class -- it keeps the plain :exc:`SessionLoginError` shape (#2329)."""
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(500)
        with pytest.raises(SessionLoginError) as exc_info:
            await vcf_session_login(
                _client,
                "/api/v2/sessions",
                username="admin",
                password="p",
                target_name="vrli-a",
                token_extractor=lambda r: r.headers.get("sessionId"),
            )
    assert not isinstance(exc_info.value, ConnectorAuthError)
    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_vcf_session_login_2xx_without_token_raises_session_login_error(
    _client: httpx.AsyncClient,
) -> None:
    """A 200 with no token in the configured location is structurally invalid.

    A misbehaving proxy or a wrong endpoint (Basic-auth-only path responding
    200 with no session header) would silently 401 every subsequent call;
    fail loudly here instead.
    """
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={})  # no sessionId header
        with pytest.raises(SessionLoginError) as exc_info:
            await vcf_session_login(
                _client,
                "/api/v2/sessions",
                username="admin",
                password="p",
                target_name="vrli-a",
                token_extractor=lambda r: r.headers.get("sessionId"),
            )
    msg = str(exc_info.value)
    assert "vrli-a" in msg
    assert "no token" in msg


@pytest.mark.asyncio
async def test_vcf_session_login_empty_string_token_treated_as_no_token(
    _client: httpx.AsyncClient,
) -> None:
    """An empty-string token from the extractor is treated as "no token".

    The contract is ``bool(token)`` — empty strings, None, 0 all fall
    through to the error. A vendor returning ``"sessionId: "`` (empty)
    is a misconfigured appliance, not a successful login.
    """
    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={}, headers={"sessionId": ""})
        with pytest.raises(SessionLoginError):
            await vcf_session_login(
                _client,
                "/api/v2/sessions",
                username="admin",
                password="p",
                target_name="vrli-a",
                token_extractor=lambda r: r.headers.get("sessionId"),
            )


# ---------------------------------------------------------------------------
# Re-login scenario covering the acceptance criterion's "401→re-login-once→
# retry-once→success / second-401→RuntimeError" shape
# ---------------------------------------------------------------------------
#
# The downstream-call 401-retry loop lives in the *consumer* (vRLI #830),
# not in vcf_auth.py. But the acceptance criterion's last clause covers the
# end-to-end story. This test simulates the consumer's loop using the
# shared helper for the login round-trip to confirm the building blocks
# fit together.


@pytest.mark.asyncio
async def test_simulated_consumer_relogin_flow_success(
    _client: httpx.AsyncClient,
) -> None:
    """Simulate a vRLI-style 401→re-login→retry-once flow ending in success.

    The consumer's loop calls ``vcf_session_login`` to get a token, makes
    a downstream call, gets 401, calls ``vcf_session_login`` again, retries
    the downstream call, and succeeds.
    """
    login_calls = 0

    def _login_handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        login_calls += 1
        return httpx.Response(200, json={}, headers={"sessionId": f"token-{login_calls}"})

    downstream_calls = 0

    def _downstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal downstream_calls
        downstream_calls += 1
        # First call 401s; second succeeds.
        if downstream_calls == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").mock(side_effect=_login_handler)
        mock.get("/api/v2/data").mock(side_effect=_downstream_handler)

        token = await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            token_extractor=lambda r: r.headers.get("sessionId"),
        )
        assert token == "token-1"

        # Consumer's downstream call.
        resp1 = await _client.get("/api/v2/data", headers={"Authorization": f"Bearer {token}"})
        assert resp1.status_code == 401

        # Consumer re-logs in.
        token = await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            token_extractor=lambda r: r.headers.get("sessionId"),
        )
        assert token == "token-2"

        # Consumer retries the downstream call.
        resp2 = await _client.get("/api/v2/data", headers={"Authorization": f"Bearer {token}"})
        assert resp2.status_code == 200

    assert login_calls == 2
    assert downstream_calls == 2


@pytest.mark.asyncio
async def test_simulated_consumer_relogin_second_401_surfaces_runtime_error(
    _client: httpx.AsyncClient,
) -> None:
    """If the re-login itself 401s, ``SessionLoginError`` surfaces with the target name.

    This is the acceptance criterion's "second-401 → RuntimeError" clause.
    The consumer's loop would catch the first downstream 401, call
    ``vcf_session_login`` again, and this raises.
    """
    login_calls = 0

    def _login_handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        login_calls += 1
        if login_calls == 1:
            return httpx.Response(200, json={}, headers={"sessionId": "token-1"})
        return httpx.Response(401, json={"error": "credentials_rotated"})

    with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").mock(side_effect=_login_handler)

        # First login succeeds.
        first_token = await vcf_session_login(
            _client,
            "/api/v2/sessions",
            username="admin",
            password="p",
            target_name="vrli-a",
            token_extractor=lambda r: r.headers.get("sessionId"),
        )
        assert first_token == "token-1"

        # Second login (simulating the consumer's re-login attempt) raises.
        with pytest.raises(SessionLoginError) as exc_info:
            await vcf_session_login(
                _client,
                "/api/v2/sessions",
                username="admin",
                password="p",
                target_name="vrli-a",
                token_extractor=lambda r: r.headers.get("sessionId"),
            )

    msg = str(exc_info.value)
    assert "vrli-a" in msg
    assert "401" in msg
