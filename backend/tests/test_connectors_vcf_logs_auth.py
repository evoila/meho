# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VcfLogsConnector` session-auth + fingerprint/probe (G3.6-T4 #830).

Exercises the load-bearing vRLI auth contract verified against the
consumer wrapper ``scripts/vcf-logs.sh`` (2026-05-21 snapshot):

* ``POST /api/v2/sessions`` with JSON body
  ``{username, password, provider}`` -- ``provider`` defaults to
  ``"Local"`` when the target leaves it unset.
* Response body ``{"sessionId": "<token>", "ttl": <seconds>}`` --
  ``sessionId`` is extracted and cached per target.
* Downstream auth header: ``Authorization: Bearer <session_id>``.
* On HTTP 401 from a downstream call, the cached session is dropped
  and a single re-login + retry-once dance follows; a second 401
  surfaces as :exc:`RuntimeError` naming the target.
* Per-target token isolation (two targets get two distinct caches).
* Credentials cache: a loader returning a dict missing ``"password"``
  surfaces a clear :exc:`RuntimeError` naming the target.
* ``auth_model != "shared_service_account"`` (or ``None``) raises
  :exc:`NotImplementedError`.
* ``fingerprint()`` against an unauthenticated ``GET /api/v2/version``
  reports the parsed version + build + release name; transport failure
  yields ``reachable=False`` with ``extras["error"]``.

The contract mirrors :mod:`tests.test_connectors_nsx_auth` with vRLI
divergence: JSON body (not form), provider field, sessionId in body
(not Set-Cookie/X-XSRF-TOKEN), Bearer header.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_logs import (
    VcfLogsConnector,
    VcfLogsTargetLike,
)


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface.

    Defaults ``raw_jwt`` to a non-empty placeholder so the cache fast-path's
    defense-in-depth fail-closed guard (``VaultCredentialsReadError`` on
    empty ``operator.raw_jwt``, mirroring the loader-path guard) doesn't
    fire on tests that don't care about the value. Tests that exercise the
    empty-jwt rejection pass ``raw_jwt=""`` explicitly.
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
def _clean_vrli_registry() -> Iterator[None]:
    """Re-register VcfLogsConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before
    every test in this module and clear after -- same pattern
    :mod:`tests.test_connectors_nsx_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=VcfLogsConnector.product,
        version=VcfLogsConnector.version,
        impl_id=VcfLogsConnector.impl_id,
        cls=VcfLogsConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub -- satisfies VcfLogsTargetLike Protocol structurally.
# Replaced by the real Target model when the provider column lands.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    provider: str | None = None


_TARGET_A = _StubTarget(
    name="vrli-a",
    host="vrli-a.test.invalid",
    port=443,
    secret_ref="kv/data/vrli/vrli-a",
)
_TARGET_B = _StubTarget(
    name="vrli-b",
    host="vrli-b.test.invalid",
    port=443,
    secret_ref="kv/data/vrli/vrli-b",
)


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VcfLogsConnector:
    """Build a connector wired with the stub loader."""
    return VcfLogsConnector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vrli_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(VcfLogsConnector, HttpConnector)
    assert VcfLogsConnector.product == "vcf-logs"
    assert VcfLogsConnector.version == "9.0"
    assert VcfLogsConnector.impl_id == "vrli-rest"
    assert VcfLogsConnector.supported_version_range == ">=9.0,<10.0"
    # Outranks a future GenericRestConnector auto-shim defensively.
    assert VcfLogsConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vcf-logs", "9.0", "vrli-rest")
    assert key in registry
    assert registry[key] is VcfLogsConnector


def test_default_credentials_loader_fails_closed_without_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default loader is the live shared operator-context Vault read (G3.10-T2).

    Empty ``raw_jwt`` is fail-closed — system-initiated calls have no
    operator JWT to forward to Vault's JWT/OIDC auth method, so the
    helper raises :class:`VaultCredentialsReadError` rather than
    silently falling back to a backplane identity. End-to-end coverage
    of the wired read lives in ``test_connectors_vcf_logs_credread.py``.
    """
    import asyncio

    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.vcf_logs.session import (
        load_credentials_from_vault,
    )
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match=r"vrli-a"):
            await load_credentials_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    try:
        asyncio.run(_check())
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Session establishment -- happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_establishes_session_with_json_body_and_default_provider() -> None:
    """First auth_headers call POSTs JSON creds + provider="Local" to /api/v2/sessions.

    Asserts the load-bearing auth contract: JSON body (NOT form, NOT
    HTTP Basic) with ``username``, ``password``, and ``provider``
    fields; ``provider`` defaults to ``"Local"`` per the wrapper's
    posture when the target leaves it unset. Bearer header lands in
    the response.
    """
    connector = _make_connector()
    session_id = "vrli-token-abc-123"

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        session_route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": session_id, "ttl": 1800}
        )
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert session_route.called and session_route.call_count == 1
    assert headers == {"Authorization": f"Bearer {session_id}"}

    request = session_route.calls[0].request
    assert request.headers.get("content-type", "").startswith("application/json")
    sent = request.read()
    import json

    body = json.loads(sent.decode())
    assert body == {
        "username": "svc-meho",
        "password": "stub-password",
        "provider": "Local",
    }
    # The login POST itself must NOT carry a stale Authorization header.
    assert "authorization" not in {k.lower() for k in request.headers}

    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_uses_target_provider_when_set() -> None:
    """target.provider="ActiveDirectory" lands in the login POST body."""
    connector = _make_connector()
    target = _StubTarget(
        name="vrli-ad",
        host="vrli-ad.test.invalid",
        port=443,
        secret_ref="kv/data/vrli/vrli-ad",
        provider="ActiveDirectory",
    )

    async with respx.mock(base_url="https://vrli-ad.test.invalid") as mock:
        session_route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": "ad-token", "ttl": 1800}
        )
        await connector.auth_headers(target, operator=_make_operator())

    request = session_route.calls[0].request
    import json

    body = json.loads(request.read().decode())
    assert body["provider"] == "ActiveDirectory"

    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_session_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-POST /api/v2/sessions."""
    connector = _make_connector()
    session_id = "vrli-cached-456"

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        session_route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": session_id, "ttl": 1800}
        )
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2 == {"Authorization": f"Bearer {session_id}"}
    # The load-bearing assertion -- exactly one POST /api/v2/sessions
    # for two auth header calls.
    assert session_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_session_tokens_separate() -> None:
    """Two targets get two distinct cached tokens; no cross-target leakage."""
    connector = _make_connector()

    async with respx.mock() as mock:
        route_a = mock.post("https://vrli-a.test.invalid/api/v2/sessions").respond(
            200, json={"sessionId": "token-a", "ttl": 1800}
        )
        route_b = mock.post("https://vrli-b.test.invalid/api/v2/sessions").respond(
            200, json={"sessionId": "token-b", "ttl": 1800}
        )

        h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    assert route_a.called and route_b.called
    assert h_a == {"Authorization": "Bearer token-a"}
    assert h_b == {"Authorization": "Bearer token-b"}
    assert connector._session_tokens == {
        "vrli-a": "token-a",
        "vrli-b": "token-b",
    }
    await connector.aclose()


# ---------------------------------------------------------------------------
# Session establishment -- failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_create_401_surfaces_session_login_error_naming_target() -> None:
    """401 from POST /api/v2/sessions raises SessionLoginError naming the target."""
    from meho_backplane.connectors.vcf_logs import SessionLoginError

    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(401, json={"errorMessage": "invalid_credentials"})
        with pytest.raises(SessionLoginError, match=r"vrli-a") as exc_info:
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "401" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_create_missing_session_id_in_body_raises() -> None:
    """A 2xx response without sessionId raises naming the target.

    A misbehaving proxy can 200 with an empty body or with the wrong
    field name; the connector fails loudly via SessionLoginError rather
    than caching an empty token that would silently 401 every
    subsequent call.
    """
    from meho_backplane.connectors.vcf_logs import SessionLoginError

    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        # Body without sessionId -- failure case.
        mock.post("/api/v2/sessions").respond(200, json={"ttl": 1800})
        with pytest.raises(SessionLoginError, match=r"vrli-a"):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    await connector.aclose()


@pytest.mark.asyncio
async def test_session_create_empty_session_id_raises() -> None:
    """A 2xx response with an empty-string sessionId raises naming the target."""
    from meho_backplane.connectors.vcf_logs import SessionLoginError

    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "", "ttl": 1800})
        with pytest.raises(SessionLoginError, match=r"vrli-a"):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_clear_error_naming_target() -> None:
    """A loader returning a dict without 'password' surfaces a clear message.

    Exercises the CredentialsCache "missing-key -> RuntimeError naming
    target" contract -- a typo in a production loader otherwise
    surfaces as a confusing KeyError deep inside the connector.
    """

    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "svc-meho"}

    connector = VcfLogsConnector(credentials_loader=_bad_loader)

    async with respx.mock(base_url="https://vrli-a.test.invalid"):
        with pytest.raises(RuntimeError, match=r"vrli-a") as exc_info:
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "password" in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="vrli-per-user",
        host="vrli.test.invalid",
        port=443,
        secret_ref="kv/data/vrli/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "vrli-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="vrli-pre-g03",
        host="vrli.test.invalid",
        port=443,
        secret_ref="kv/data/vrli/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "pre-g03-token", "ttl": 1800})
        headers = await connector.auth_headers(target, operator=_make_operator())

    assert headers == {"Authorization": "Bearer pre-g03-token"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_value_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="vrli-enum",
        host="vrli.test.invalid",
        port=443,
        secret_ref="kv/data/vrli/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "enum-token", "ttl": 1800})
        headers = await connector.auth_headers(target, operator=_make_operator())

    assert headers == {"Authorization": "Bearer enum-token"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# 401 -> re-login -> retry-once recovery (downstream call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downstream_401_triggers_relogin_and_retry_once() -> None:
    """A 401 on a downstream GET triggers session invalidation + re-login + a single retry.

    Exercises the issue's "on HTTP 401 from a downstream call, re-login
    once then retry once" contract. The session-create route fires
    twice (initial + post-401); the downstream route fires twice (401
    then 200).
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        session_route = mock.post("/api/v2/sessions")
        session_route.side_effect = [
            httpx.Response(200, json={"sessionId": "token-first", "ttl": 1800}),
            httpx.Response(200, json={"sessionId": "token-second", "ttl": 1800}),
        ]
        events_route = mock.get("/api/v2/events")
        events_route.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"events": [{"id": "ev-1"}]}),
        ]

        result = await connector._get_json_with_session_retry(
            _TARGET_A, "/api/v2/events", operator=_make_operator()
        )

    assert result == {"events": [{"id": "ev-1"}]}
    # Re-login fired exactly once -- two POSTs total.
    assert session_route.call_count == 2
    # Downstream GET fired twice -- the original 401 + the post-relogin retry.
    assert events_route.call_count == 2
    # The post-relogin token replaced the stale one.
    assert connector._session_tokens == {"vrli-a": "token-second"}
    # Both downstream GETs carried Bearer headers (first the stale
    # token, second the refreshed one).
    requests = events_route.calls
    assert requests[0].request.headers.get("authorization") == "Bearer token-first"
    assert requests[1].request.headers.get("authorization") == "Bearer token-second"
    await connector.aclose()


@pytest.mark.asyncio
async def test_downstream_401_then_still_401_after_relogin_raises_runtime_error() -> None:
    """If the post-relogin retry also 401s, RuntimeError naming the target is raised.

    Single retry, not a loop -- a configured credential pair that
    consistently 401s should fail fast rather than hammering vRLI's
    audit log.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        session_route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": "any-token", "ttl": 1800}
        )
        events_route = mock.get("/api/v2/events").respond(401)

        with pytest.raises(RuntimeError, match=r"vrli-a") as exc_info:
            await connector._get_json_with_session_retry(
                _TARGET_A, "/api/v2/events", operator=_make_operator()
            )

    assert "401" in str(exc_info.value)
    assert "after refresh" in str(exc_info.value)
    # Exactly one re-login attempt + two GETs (no further retries).
    assert session_route.call_count == 2
    assert events_route.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_downstream_non_401_status_error_propagates_untouched() -> None:
    """A 500 (or any non-401 status) on a downstream call does NOT trigger relogin.

    Re-establishing the session on a 5xx would mask transient backend
    failures behind a credential-rotation that solves nothing; the
    relogin branch is keyed strictly on 401.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        session_route = mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": "any-token", "ttl": 1800}
        )
        # 502 will exhaust the base ``_request_json`` tenacity budget
        # (3 retries on 5xx) before raising; assert "exactly one POST"
        # below to guarantee we never triggered a relogin.
        mock.get("/api/v2/events").respond(502)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector._get_json_with_session_retry(
                _TARGET_A, "/api/v2/events", operator=_make_operator()
            )

    assert exc_info.value.response.status_code == 502
    # One session-create call (initial); no re-login fired.
    assert session_route.call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() + probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against a respx-mocked GET /api/v2/version returns the canonical shape.

    Mirrors the wrapper's ``--probe`` output shape: the ``version``
    field is the dot-separated value parts[0:3], ``build`` is
    parts[4], ``extras["patch"]`` is parts[3], and the full string
    lands in ``extras["version_full"]``. The endpoint is hit
    unauthenticated -- no session-create POST is expected.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid", assert_all_called=False) as mock:
        version_route = mock.get("/api/v2/version").respond(
            200,
            json={
                "version": "9.0.0.0.21761695",
                "releaseName": "VMware Aria Operations for Logs 9.0",
            },
        )
        # Defensive: if the connector unexpectedly tries to auth, we
        # want a loud failure, not a successful 200 -- assert_all_called
        # is disabled so this unused route doesn't make the test green
        # the wrong way.
        session_route = mock.post("/api/v2/sessions").respond(500)

        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-logs"
    assert fp.version == "9.0.0"
    assert fp.build == "21761695"
    assert fp.reachable is True
    assert fp.probe_method == "GET /api/v2/version"
    assert fp.extras["release_name"] == "VMware Aria Operations for Logs 9.0"
    assert fp.extras["version_full"] == "9.0.0.0.21761695"
    assert fp.extras["patch"] == "0"
    # Probe is unauthenticated -- session-create was never called.
    assert version_route.called
    assert not session_route.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_error() -> None:
    """Transport/status failure returns reachable=False with structured extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        # 500 on the version endpoint -- the HTTPStatusError surfaces
        # as a structured reachable=False rather than propagating up.
        mock.get("/api/v2/version").respond(500, json={"error": "internal"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-logs"
    assert fp.reachable is False
    assert fp.probe_method == "GET /api/v2/version"
    # Structured error: ``"<ExcType>: <message>"``.
    error = fp.extras["error"]
    assert "HTTPStatusError" in error
    assert "500" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_reachable() -> None:
    """probe() returns ok=True on a reachable target (delegates to fingerprint)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.get("/api/v2/version").respond(
            200,
            json={"version": "9.0.0.0.21761695", "releaseName": "Logs"},
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_when_unreachable() -> None:
    """probe() returns ok=False + reason on an unreachable target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.get("/api/v2/version").respond(500, json={"error": "internal"})
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "500" in result.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose -- token + credentials cache clear + pool tear-down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_session_token_cache_and_credentials_and_pool() -> None:
    """aclose() clears in-memory session + credentials caches and tears down the httpx pool."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrli-a.test.invalid") as mock:
        mock.post("/api/v2/sessions").respond(200, json={"sessionId": "token", "ttl": 1800})
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert connector._session_tokens == {"vrli-a": "token"}
    # Credentials cache should also be populated after the first call.
    assert "vrli-a" in connector._credentials.cached_targets
    await connector.aclose()
    assert connector._session_tokens == {}
    assert connector._credentials.cached_targets == frozenset()
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_sessions_is_a_noop() -> None:
    """A fresh connector with no sessions established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._session_tokens == {}


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_stub_target_satisfies_vcf_logs_target_like_protocol() -> None:
    """The runtime-checkable Protocol accepts the stub target.

    Sanity-checks that ``_StubTarget`` (which mirrors the shape the
    concrete Target model will expose once the ``provider`` column
    lands) satisfies :class:`VcfLogsTargetLike` structurally -- no
    explicit inheritance required.
    """
    assert isinstance(_TARGET_A, VcfLogsTargetLike)
