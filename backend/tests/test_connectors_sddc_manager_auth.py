# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`SddcManagerConnector` auth + fingerprint/probe.

Exercises the ``session_login_token`` auth flow (#2290): the connector
POSTs ``{username, password}`` to ``/v1/tokens``, reads ``accessToken`` out
of the response, and sends it as ``Authorization: Bearer <accessToken>`` on
every subsequent request — SDDC Manager rejects HTTP Basic outright. Covers
per-target credential + token isolation, the system-operator cache bypass
(#1008), the auth_model boundary gate, single re-login on a downstream 401,
and the fingerprint/probe shape against a mocked ``GET /v1/sddc-managers``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    SYSTEM_OPERATOR_SUB,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError, SessionLoginError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.sddc_manager import (
    SddcManagerConnector,
    SddcTargetLike,
)

_TOKEN_PATH = "/v1/tokens"


def _make_operator(raw_jwt: str = "") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_sddc_registry() -> Iterator[None]:
    """Re-register SddcManagerConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before every
    test in this module and clear after — same pattern
    :mod:`tests.test_connectors_nsx_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=SddcManagerConnector.product,
        version=SddcManagerConnector.version,
        impl_id=SddcManagerConnector.impl_id,
        cls=SddcManagerConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies SddcTargetLike Protocol structurally.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None  # #2398: per-target TLS SNI / cert-verify name
    # Tenant-unique cache key components (#1642). Distinct ``id`` per
    # instance so two stub targets never collapse onto one cache entry.
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="sddc-a",
    host="sddc-a.test.invalid",
    port=443,
    secret_ref="sddc/sddc-a",
)
_TARGET_B = _StubTarget(
    name="sddc-b",
    host="sddc-b.test.invalid",
    port=443,
    secret_ref="sddc/sddc-b",
)


async def _stub_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target or operator."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> SddcManagerConnector:
    """Build a connector wired with the stub loader."""
    return SddcManagerConnector(credentials_loader=_stub_loader)


def _mint_from_username(
    token_prefix: str = "tok",
) -> Callable[[httpx.Request], httpx.Response]:
    """respx side-effect minting ``<prefix>-<login-body-username>`` as the token.

    Lets a single ``/v1/tokens`` route return a token that varies with the
    posted credential body, so per-target / per-tenant token distinctness can
    be asserted from one router.
    """

    def _mint(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"accessToken": f"{token_prefix}-{body['username']}"})

    return _mint


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_sddc_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(SddcManagerConnector, HttpConnector)
    assert SddcManagerConnector.product == "sddc"
    assert SddcManagerConnector.version == "9.0"
    assert SddcManagerConnector.impl_id == "sddc-rest"
    assert SddcManagerConnector.supported_version_range == ">=9.0,<10.0"
    assert SddcManagerConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("sddc", "9.0", "sddc-rest")
    assert key in registry
    assert registry[key] is SddcManagerConnector


def test_default_credentials_loader_delegates_to_shared_basic_loader() -> None:
    """The default loader is the thin wrapper around ``load_basic_credentials``.

    G3.10-T1 (#945) wired the live read; the loader now delegates to
    :func:`load_basic_credentials` rather than raising
    :exc:`NotImplementedError`. The fail-closed precondition (empty
    ``operator.raw_jwt``) is asserted via a :class:`VaultCredentialsReadError`
    on a system-initiated synthetic operator (no Vault is touched).
    """
    import asyncio

    from meho_backplane.connectors.sddc_manager.session import load_credentials_from_vault

    async def _check() -> None:
        system_operator = _make_operator(raw_jwt="")
        with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
            await load_credentials_from_vault(_TARGET_A, system_operator)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# session_login_token auth — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_mints_and_sends_bearer_token() -> None:
    """auth_headers() POSTs /v1/tokens and returns Authorization: Bearer <accessToken>.

    No request ever carries HTTP Basic — the appliance rejects it.
    """
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert headers == {"Authorization": "Bearer tok-abc"}
    assert token_route.call_count == 1
    # The login POST carried the JSON credential body — no Basic header.
    login_request = token_route.calls[0].request
    assert login_request.headers.get("authorization") is None
    assert json.loads(login_request.content) == {
        "username": "svc-meho",
        "password": "stub-password",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_token_across_calls() -> None:
    """Second auth_headers call reuses the cached token — no re-mint, no re-load."""
    call_count = 0

    async def _counting_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2 == {"Authorization": "Bearer tok-abc"}
    assert call_count == 1
    assert token_route.call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# System-operator cache-bypass (fail-closed; #1008)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_cache_not_served_to_system_operator_runs_loader() -> None:
    """A warm session primed by a real operator is NOT served to the system operator.

    The system/operator-less caller (``synthesise_system_operator``) must
    re-run the loader so its fail-closed behaviour applies — it can never
    borrow a warm credential/token a real operator resolved (#1008). Keyed off
    ``SYSTEM_OPERATOR_SUB``, not ``raw_jwt`` (the system operator carries a
    non-empty placeholder JWT since #980).
    """
    call_log: list[str] = []

    async def _counting_loader(_target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        call_log.append(operator.sub)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        # Real operator warms the cache (cold load + mint).
        await connector.auth_headers(_TARGET_A, operator=_make_operator())
        # System operator must re-run the loader rather than reuse the warm cache.
        await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())

    assert call_log == ["test-operator", SYSTEM_OPERATOR_SUB]
    await connector.aclose()


@pytest.mark.asyncio
async def test_system_operator_fails_closed_against_warm_cache() -> None:
    """With a warm cache, a system-operator load runs the loader and fails closed.

    Proves the bypass is closed end to end: even though a real operator
    primed the cache, the system caller hits the loader, which fails closed
    per its contract (a system-initiated read cannot resolve per-target
    credentials).
    """

    async def _failing_for_system_loader(
        _target: SddcTargetLike, operator: Operator
    ) -> dict[str, str]:
        if operator.sub == SYSTEM_OPERATOR_SUB:
            raise VaultCredentialsReadError(
                "system-initiated calls cannot read per-target vendor credentials"
            )
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_failing_for_system_loader)
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm the cache
        with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
            await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_real_operator_reuse_unchanged_after_system_operator_call() -> None:
    """Real-operator reuse is unaffected by the system-operator cache bypass.

    A second real-operator call still reuses the warm cache (loader called
    once for the real operator); only the interleaved system-operator call
    re-runs the loader.
    """
    real_calls = 0

    async def _counting_loader(_target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        nonlocal real_calls
        if operator.sub != SYSTEM_OPERATOR_SUB:
            real_calls += 1
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        await connector.auth_headers(_TARGET_A, operator=_make_operator())  # cold real load
        await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())  # bypass
        await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm real reuse

    assert real_calls == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_tokens_separate() -> None:
    """Two targets get two distinct credential + token cache entries; no leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = SddcManagerConnector(credentials_loader=_tracking_loader)
    async with respx.mock(assert_all_called=False) as mock:
        mock.post("https://sddc-a.test.invalid/v1/tokens").mock(side_effect=_mint_from_username())
        mock.post("https://sddc-b.test.invalid/v1/tokens").mock(side_effect=_mint_from_username())
        h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    assert h_a == {"Authorization": "Bearer tok-svc-sddc-a"}
    assert h_b == {"Authorization": "Bearer tok-svc-sddc-b"}
    # Loader called exactly once per distinct target.
    assert call_log == ["sddc-a", "sddc-b"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_tokens() -> None:
    """Same-named targets in DIFFERENT tenants never share a cached credential/token.

    Regression guard for #1642/#1672/#1684: the caches key on the
    tenant-unique ``(tenant_id, id)`` tuple, so two same-named targets in
    different tenants get distinct credential + token entries and one tenant
    can never be served another tenant's cached token.
    """
    load_count = 0

    async def _counting_loader(target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal load_count
        load_count += 1
        return {"username": f"svc-{target.tenant_id}", "password": "pass"}

    tenant_one = _StubTarget(
        name="sddc-shared",
        host="sddc-shared.test.invalid",
        port=443,
        secret_ref="sddc/sddc-shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="sddc-shared",
        host="sddc-shared.test.invalid",
        port=443,
        secret_ref="sddc/sddc-shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    async with respx.mock(base_url="https://sddc-shared.test.invalid") as mock:
        mock.post(_TOKEN_PATH).mock(side_effect=_mint_from_username())
        h_one = await connector.auth_headers(tenant_one, operator=_make_operator())
        h_two = await connector.auth_headers(tenant_two, operator=_make_operator())

        # Each tenant triggered its own load + mint — no cross-tenant cache hit.
        assert load_count == 2
        assert h_one == {"Authorization": f"Bearer tok-svc-{tenant_one.tenant_id}"}
        assert h_two == {"Authorization": f"Bearer tok-svc-{tenant_two.tenant_id}"}
        assert h_one != h_two
        assert connector._creds_cache.keys() == {
            target_cache_key(tenant_one),
            target_cache_key(tenant_two),
        }
        assert connector._session_tokens.keys() == {
            target_cache_key(tenant_one),
            target_cache_key(tenant_two),
        }

        # Same-tenant re-fetch is a cache HIT — behaviour unchanged.
        h_one_again = await connector.auth_headers(tenant_one, operator=_make_operator())
        assert h_one_again == h_one
        assert load_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Session-login failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_non_2xx_raises_session_login_error_naming_target() -> None:
    """A non-2xx from /v1/tokens surfaces a SessionLoginError naming the target."""
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(401, json={"message": "bad credentials"})
        with pytest.raises(SessionLoginError, match=r"sddc-a"):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_without_access_token_raises_session_login_error() -> None:
    """A 2xx body carrying no accessToken surfaces a SessionLoginError."""
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"refreshToken": {"id": "r"}})
        with pytest.raises(SessionLoginError, match=r"sddc-a"):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes (raised before the login POST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    """A loader returning a dict without 'password' surfaces a clear RuntimeError."""

    async def _bad_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "svc-meho"}  # type: ignore[return-value]

    connector = SddcManagerConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "sddc-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    """A loader returning a dict without 'username' surfaces a clear RuntimeError."""

    async def _bad_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        return {"password": "stub-password"}  # type: ignore[return-value]

    connector = SddcManagerConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "sddc-a" in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating (rejected before any login POST fires)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="sddc-per-user",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "sddc-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="sddc-pre-g03",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "Bearer tok-abc"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="sddc-enum",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers == {"Authorization": "Bearer tok-abc"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# invalidate_session — the #2067 dispatch-path seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_session_evicts_token_and_re_mints_on_next_call() -> None:
    """invalidate_session drops the cached token; the next call re-establishes it."""
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        await connector.auth_headers(_TARGET_A, operator=_make_operator())
        assert target_cache_key(_TARGET_A) in connector._session_tokens
        assert token_route.call_count == 1

        await connector.invalidate_session(_TARGET_A)
        assert target_cache_key(_TARGET_A) not in connector._session_tokens

        await connector.auth_headers(_TARGET_A, operator=_make_operator())
        assert token_route.call_count == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# invalidate_credentials — the #2396 establish-auth-failure eviction seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_credentials_reloads_after_establish_401_restage() -> None:
    """#2396 red-today: a restaged credential is re-read after establish-auth eviction.

    The credential cache is written *before* the login POST (``connector.py``
    caches ``_creds_cache`` then calls ``POST /v1/tokens``), so a login that
    401s leaves the rejected bytes cached and ``invalidate_session`` leaves
    them intact by design. Without an eviction path every re-dispatch replays
    the rejected credential until a restart. This test proves the fix: the
    first login 401s -> ``ConnectorAuthError`` (loader call 1); the dispatcher
    calls ``invalidate_credentials``; the operator restages the correct
    password; the next ``auth_headers`` re-runs the loader (call-count 2) and
    the login succeeds -- no backplane restart. Red before #2396: the method
    did not exist.
    """
    calls = {"n": 0}
    creds = {"username": "svc-old", "password": "wrong"}

    async def _swappable_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        calls["n"] += 1
        return dict(creds)

    def _login(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["password"] == "correct":
            return httpx.Response(200, json={"accessToken": f"tok-{body['username']}"})
        return httpx.Response(401, json={"message": "bad credentials"})

    connector = SddcManagerConnector(credentials_loader=_swappable_loader)
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).mock(side_effect=_login)

        # First dispatch: the wrong staged password is cached, then rejected.
        with pytest.raises(ConnectorAuthError):
            await connector.auth_headers(_TARGET_A, operator=_make_operator())
        assert calls["n"] == 1
        assert target_cache_key(_TARGET_A) in connector._creds_cache

        # The dispatcher's establish-auth arm evicts the cached credential.
        await connector.invalidate_credentials(_TARGET_A)
        assert target_cache_key(_TARGET_A) not in connector._creds_cache

        # The operator restages the corrected credential out of band.
        creds.update(username="svc-new", password="correct")

        # The next dispatch re-reads the store (call-count 2) and succeeds.
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        assert calls["n"] == 2
        assert headers == {"Authorization": "Bearer tok-svc-new"}
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() + probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() authenticates via the token flow then reads GET /v1/sddc-managers."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        managers_route = mock.get("/v1/sddc-managers").respond(
            200,
            json={
                "elements": [
                    {
                        "id": "sddc-uuid-1",
                        "fqdn": "sddc-a.test.invalid",
                        "version": "9.0.0.0-24276214",
                        "build": "24276214",
                        "domain": {"id": "domain-uuid-1", "name": "MGMT"},
                    }
                ],
                "pageMetadata": {"pageNumber": 1, "pageSize": 10, "totalElements": 1},
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "sddc"
    assert fp.version == "9.0.0.0-24276214"
    assert fp.build == "24276214"
    assert fp.reachable is True
    assert fp.probe_method == "GET /v1/sddc-managers"
    assert fp.extras["management_domain"] == "MGMT"
    assert fp.extras["management_domain_id"] == "domain-uuid-1"
    assert fp.extras["id"] == "sddc-uuid-1"
    assert fp.extras["fqdn"] == "sddc-a.test.invalid"
    # The inventory GET carried the minted Bearer token — never HTTP Basic.
    inv_auth = managers_route.calls[0].request.headers.get("authorization")
    assert inv_auth == "Bearer tok-abc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_recovers_once_from_a_401_then_reports_reachable() -> None:
    """A single 401 on the inventory GET recovers via one re-login; a second 401 fails.

    The helper-level ``_get_json_with_session_retry`` evicts the token, re-mints,
    and retries the GET once. Here the retry succeeds, so the fingerprint is
    reachable.
    """
    connector = _make_connector()
    inventory = {
        "elements": [
            {
                "id": "x",
                "fqdn": "sddc-a.test.invalid",
                "version": "9.0.0.0",
                "domain": {"name": "M"},
            }
        ]
    }
    calls = {"n": 0}

    def _inv(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"message": "token expired"})
        return httpx.Response(200, json=inventory)

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").mock(side_effect=_inv)
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is True
    assert fp.version == "9.0.0.0"
    # One initial mint + one re-mint after the 401 eviction.
    assert token_route.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_on_persistent_401() -> None:
    """A persistent 401 (re-login also 401s) returns reachable=False with a structured error."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").respond(401, json={"message": "Unauthorized"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is False
    assert fp.probe_method == "GET /v1/sddc-managers"
    assert "401" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_empty_elements_returns_reachable_true_with_no_version() -> None:
    """An empty elements list produces reachable=True with version=None (API responded)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").respond(
            200,
            json={"elements": [], "pageMetadata": {"totalElements": 0}},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is True
    assert fp.version is None
    assert fp.extras["management_domain"] is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_reachable() -> None:
    """probe() returns ok=True on a reachable target (delegates to fingerprint)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").respond(
            200,
            json={
                "elements": [
                    {
                        "id": "u",
                        "fqdn": "sddc-a.test.invalid",
                        "version": "9.0.0.0",
                        "domain": {"id": "d", "name": "MGMT"},
                    }
                ]
            },
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_when_unreachable() -> None:
    """probe() returns ok=False + reason on an unreachable target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").respond(401)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose — credential + token cache clear + pool tear-down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_caches_and_pool() -> None:
    """aclose() clears the in-memory credential + token caches and tears down the pool."""
    connector = _make_connector()
    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert target_cache_key(_TARGET_A) in connector._creds_cache
    assert target_cache_key(_TARGET_A) in connector._session_tokens
    await connector.aclose()
    assert connector._creds_cache == {}
    assert connector._session_tokens == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_state_is_a_noop() -> None:
    """A fresh connector with no session established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._creds_cache == {}
    assert connector._session_tokens == {}


# ---------------------------------------------------------------------------
# G0.16-T4 (#1306) probe-vs-dispatch convergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_forwards_route_operator_to_credentials_loader() -> None:
    """G0.16-T4 (#1306) probe-vs-dispatch convergence regression for sddc-manager.

    Post-#1306 the probe route forwards its operator — the same code path the
    dispatch surface uses. Test pins:
    1. The credentials loader receives the route operator.
    2. The forwarded JWT has the compact-JWS shape (≥3 dot-separated parts).
    """
    captured: list[Operator] = []

    async def _capturing_loader(
        _target: SddcTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        captured.append(operator)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_capturing_loader)

    route_operator = Operator(
        sub="op-rdc",
        name="RDC Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").respond(
            200,
            json={
                "elements": [
                    {
                        "id": "sddc-uuid-1",
                        "fqdn": "sddc-a.test.invalid",
                        "version": "9.0.0.0-24276214",
                        "build": "24276214",
                        "domain": {"id": "domain-uuid-1", "name": "MGMT"},
                    }
                ],
                "pageMetadata": {"pageNumber": 1, "pageSize": 10, "totalElements": 1},
            },
        )
        await connector.fingerprint(_TARGET_A, operator=route_operator)

    assert len(captured) == 1
    fwd = captured[0]
    assert fwd.sub == route_operator.sub
    assert len(fwd.raw_jwt.split(".")) >= 3, (
        "forwarded JWT must look like a compact-JWS so Vault accepts it"
    )
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_operator_falls_back_to_system_operator() -> None:
    """``fingerprint(target)`` without ``operator`` synthesises the system operator."""
    captured: list[Operator] = []

    async def _capturing_loader(
        _target: SddcTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        captured.append(operator)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_capturing_loader)

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        mock.get("/v1/sddc-managers").respond(
            200,
            json={"elements": [], "pageMetadata": {}},
        )
        await connector.fingerprint(_TARGET_A)

    assert len(captured) == 1
    assert captured[0].sub == SYSTEM_OPERATOR_SUB
    await connector.aclose()


# ---------------------------------------------------------------------------
# TLS SNI / cert-verify override on the SDDC /v1/tokens login (#2398)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_login_threads_sni_extension() -> None:
    """#2398: the SDDC Manager /v1/tokens login POST carries the SNI override."""
    connector = _make_connector()
    target = _StubTarget(
        name="sddc-sni",
        host="sddc-sni.test.invalid",
        port=443,
        secret_ref="sddc/sddc-sni",
        tls_server_name="sddc.corp.example",
    )

    async with respx.mock(base_url="https://sddc-sni.test.invalid") as mock:
        route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "tok-abc"})
        await connector.auth_headers(target, operator=_make_operator())

    assert route.called
    assert route.calls[0].request.extensions["sni_hostname"] == "sddc.corp.example"
    await connector.aclose()
