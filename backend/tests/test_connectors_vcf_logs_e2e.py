# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vRLI E2E recorded-fixture integration test (G3.6-T6 #838).

Covers the four acceptance criteria from Issue #838:

(a) All 7 curated vRLI read ops dispatch through the full
    ``call_operation`` stack against a respx-mocked vRLI appliance and
    return ``status='ok'``. Each call exercises session-establish
    (first call) or re-uses the cached session token (subsequent
    calls).

(b) Session-establish + 401-retry path — the **key load-bearing E2E**
    for vRLI's session-token flavour. Exercised through
    :meth:`VcfLogsConnector._get_json_with_session_retry` (the call
    site for ``fingerprint`` / ``probe`` and any future typed ops that
    need the retry-once contract). The respx mock orchestrates:

    * Session-create returns ``sessionId=canary-vrli-session-token``
      (initial establish).
    * Downstream ``GET /api/v2/version`` returns 401 (simulated session
      expiry).
    * The connector invalidates its cached token and POSTs to
      ``/api/v2/sessions`` a second time, receiving
      ``sessionId=canary-vrli-session-token-refreshed``.
    * Downstream ``GET /api/v2/version`` returns 200 on the retry.

    Asserts session-create called exactly twice, downstream GET called
    exactly twice, post-retry token cache holds the refreshed id.

    The second test in this pair (second-401-fails path) drives the
    contract's failure mode: when the post-relogin retry also returns
    401, the connector raises ``RuntimeError`` naming the target —
    "re-login once on session-expiry, not a retry loop". Asserts the
    error message names the target and references the 401.

(c) Audit rows — each dispatch inserts an ``AuditLog`` row carrying
    ``method='DISPATCH'``, a non-null ``target_id``, and a
    ``payload["params_hash"]`` key.

(d) JSONFlux handle path — ``GET:/api/v2/events/{constraints}``
    dispatched with the real
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    in force mode (``row_threshold=0``) returns a populated
    ``OperationResult.handle`` with at least one ``sample_rows``
    entry. Asserts the reducer-summarised envelope carries
    ``row_count`` matching the seeded event count.

Database: SQLite via the autouse ``_default_database_url`` conftest
fixture (no ``pg_engine`` required for the 401-retry tests; the
canary acceptance fixture provides its own DB setup). Runs in the
``meho-runners`` CI lane alongside the other
``backend/tests/test_connectors_*.py`` files; no Docker dependency.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.vcf_logs import (
    VRLI_CONNECTOR_ID,
    VRLI_IMPL_ID,
    VRLI_VERSION,
    VcfLogsConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._vrli_canary_fixtures import (
    VRLI_CANARY_BASE_URL,
    VRLI_CANARY_CORE_OP_IDS,
    VRLI_CANARY_EVENTS,
    VRLI_CANARY_FINGERPRINT,
    VRLI_CANARY_OPERATOR_TENANT,
    VRLI_CANARY_SESSION_ID,
    VRLI_CANARY_SESSION_REFRESH_ID,
    VRLI_CONSTRAINT_OP_PARAMS,
    VRLI_FORCE_HANDLE_LIST_OP_ID,
    VRLI_RESERVED_CONSTRAINT_OP_ID,
    VRLI_RESERVED_CONSTRAINT_VALUE,
    _insert_vrli_descriptors,
    _insert_vrli_reserved_constraint_descriptor,
    _register_vrli_reserved_constraint_route,
    _register_vrli_routes,
    _vrli_credentials_loader,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_OPERATOR = Operator(
    sub="vrli-e2e-test",
    name="vRLI E2E Test Operator",
    email=None,
    raw_jwt="<vrli-e2e-raw-jwt>",
    tenant_id=VRLI_CANARY_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_E2E_TARGET_NAME = "vrli-e2e-target"


# ---------------------------------------------------------------------------
# Module-level autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars that :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

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
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub out :func:`publish_event` so the broadcast bus doesn't fire."""
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------


async def _seed_target() -> Any:
    """Insert the E2E target row and return it (expunged from the session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=VRLI_CANARY_OPERATOR_TENANT,
            name=_E2E_TARGET_NAME,
            aliases=[],
            # ``Target.product`` binds via the resolver to the v2 registry
            # triple ``(VcfLogsConnector.product, "9.0", "vrli-rest")``.
            # Since G0.26-T4 (#1798) aligned the connector to the
            # dispatch-canonical token, ``VcfLogsConnector.product`` now
            # EQUALS ``VRLI_PRODUCT`` (both ``"vrli"``) — the target, the
            # ingested rows, and the registration all share one product
            # namespace, which is the fix for the v0.16.0 SEV-2.
            product=VcfLogsConnector.product,
            host=VRLI_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="vrli/vrli-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=VRLI_CANARY_FINGERPRINT,
            notes="seeded by test_connectors_vcf_logs_e2e._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> VcfLogsConnector:
    """Resolve + cache the VcfLogsConnector instance with the stub loader.

    Looks up the v2 registry under the **connector class metadata**
    triple ``(VcfLogsConnector.product, version, impl_id)`` — which,
    after the G0.26-T4 (#1798) alignment, is
    ``("vrli", "9.0", "vrli-rest")``. ``VcfLogsConnector.product`` now
    equals ``VRLI_PRODUCT`` (``"vrli"``) and equals the product
    ``parse_connector_id`` reads off the ``connector_id`` slug
    (``"vrli-rest-9.0"`` → ``product="vrli"``): one canonical identity
    across the class, the rows, and the operator target.
    """
    registry = all_connectors_v2()
    registry_key = (VcfLogsConnector.product, VRLI_VERSION, VRLI_IMPL_ID)
    connector_cls = registry.get(registry_key)
    if connector_cls is None:
        # The connector package registers itself at import time; reload
        # in case a sibling test wiped the v2 registry.
        import importlib

        import meho_backplane.connectors.vcf_logs as _vrli_pkg

        importlib.reload(_vrli_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get(registry_key)
    assert connector_cls is VcfLogsConnector, (
        f"VcfLogsConnector not registered for {registry_key!r}; got {connector_cls!r}"
    )
    instance = get_or_create_connector_instance(connector_cls)
    # The CredentialsCache wraps the loader callable; swap it in place
    # so the canary's stub returns instead of the not-yet-wired Vault
    # NotImplementedError. Also clear any stale token cache from a
    # prior test's connector reuse.
    instance._credentials._loader = _vrli_credentials_loader  # type: ignore[attr-defined]
    instance._session_tokens.clear()
    return instance


# ---------------------------------------------------------------------------
# Primary E2E fixture (happy-path + audit rows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _VrliE2EBundle:
    target_name: str
    connector_instance: VcfLogsConnector
    db_target: Any


@pytest.fixture
async def vrli_e2e_canary(captured_events: list[Any]) -> AsyncIterator[_VrliE2EBundle]:
    """Dispatcher-ready vRLI setup over a respx-mocked appliance (happy-path)."""
    del captured_events  # the fixture's side-effect is the patched publisher

    await _insert_vrli_descriptors()
    seeded_target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vrli_routes(mock)
        try:
            yield _VrliE2EBundle(
                target_name=_E2E_TARGET_NAME,
                connector_instance=instance,
                db_target=seeded_target,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# 401-retry fixtures (acceptance criterion b — load-bearing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Vrli401RetryBundle:
    connector_instance: VcfLogsConnector
    db_target: Any
    session_route: Any
    version_route: Any


@pytest.fixture
async def vrli_e2e_401_recovery(
    captured_events: list[Any],
) -> AsyncIterator[_Vrli401RetryBundle]:
    """vRLI setup that simulates a single 401 on a downstream call.

    The session-create route answers twice (initial establish +
    post-401 re-login). ``GET /api/v2/version`` returns 401 first then
    200, exercising the single-retry contract. Each session POST
    returns a distinct ``sessionId`` so tests can assert the cache is
    invalidated + refreshed (not stale-served).
    """
    del captured_events

    await _insert_vrli_descriptors()
    target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        session_route = mock.post("/api/v2/sessions")
        session_route.side_effect = [
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800},
            ),
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_REFRESH_ID, "ttl": 1800},
            ),
        ]
        version_route = mock.get("/api/v2/version")
        version_route.side_effect = [
            httpx.Response(401, json={"errorMessage": "session_expired"}),
            httpx.Response(
                200,
                json={
                    "version": "9.0.0",
                    "releaseName": "VMware Aria Operations for Logs 9.0",
                    "buildNumber": "21761695",
                },
            ),
        ]
        try:
            yield _Vrli401RetryBundle(
                connector_instance=instance,
                db_target=target,
                session_route=session_route,
                version_route=version_route,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


@pytest.fixture
async def vrli_e2e_401_persists(
    captured_events: list[Any],
) -> AsyncIterator[_Vrli401RetryBundle]:
    """vRLI setup that returns 401 from the downstream call even after re-login.

    Drives the second-401-fails contract: when a fresh session token
    still produces a 401 on the downstream call, the connector raises
    ``RuntimeError`` naming the target rather than entering a retry
    loop.
    """
    del captured_events

    await _insert_vrli_descriptors()
    target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        session_route = mock.post("/api/v2/sessions")
        session_route.side_effect = [
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800},
            ),
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_REFRESH_ID, "ttl": 1800},
            ),
        ]
        version_route = mock.get("/api/v2/version")
        version_route.side_effect = [
            httpx.Response(401, json={"errorMessage": "session_expired"}),
            httpx.Response(401, json={"errorMessage": "still_unauthorised"}),
        ]
        try:
            yield _Vrli401RetryBundle(
                connector_instance=instance,
                db_target=target,
                session_route=session_route,
                version_route=version_route,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


@pytest.fixture
async def vrli_e2e_440_recovery(
    captured_events: list[Any],
) -> AsyncIterator[_Vrli401RetryBundle]:
    """vRLI setup that simulates a single 440 (session expired) on a downstream call.

    Mirrors :func:`vrli_e2e_401_recovery` but the downstream
    ``GET /api/v2/version`` returns vRLI's own **440** (``"Login
    Timeout"`` / session-expired) on the first attempt, then 200. This is
    the case observed live on v0.17.0 (#1909): vRLI idle-expires the
    in-memory session, so the call after an idle gap returns 440. The
    connector must treat 440 exactly like 401 — invalidate the cached
    token, re-login via ``POST /api/v2/sessions``, retry once.
    """
    del captured_events

    await _insert_vrli_descriptors()
    target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        session_route = mock.post("/api/v2/sessions")
        session_route.side_effect = [
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800},
            ),
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_REFRESH_ID, "ttl": 1800},
            ),
        ]
        version_route = mock.get("/api/v2/version")
        version_route.side_effect = [
            httpx.Response(440, json={"errorMessage": "Login Timeout"}),
            httpx.Response(
                200,
                json={
                    "version": "9.0.0",
                    "releaseName": "VMware Aria Operations for Logs 9.0",
                    "buildNumber": "21761695",
                },
            ),
        ]
        try:
            yield _Vrli401RetryBundle(
                connector_instance=instance,
                db_target=target,
                session_route=session_route,
                version_route=version_route,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


@pytest.fixture
async def vrli_e2e_440_persists(
    captured_events: list[Any],
) -> AsyncIterator[_Vrli401RetryBundle]:
    """vRLI setup that returns 440 from the downstream call even after re-login.

    The failure half of the 440 contract: a fresh session token that
    still produces a 440 on the downstream call raises ``RuntimeError``
    naming the target and the status rather than entering a retry loop —
    same fast-fail posture as the second-401 case.
    """
    del captured_events

    await _insert_vrli_descriptors()
    target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        session_route = mock.post("/api/v2/sessions")
        session_route.side_effect = [
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800},
            ),
            httpx.Response(
                200,
                json={"sessionId": VRLI_CANARY_SESSION_REFRESH_ID, "ttl": 1800},
            ),
        ]
        version_route = mock.get("/api/v2/version")
        version_route.side_effect = [
            httpx.Response(440, json={"errorMessage": "Login Timeout"}),
            httpx.Response(440, json={"errorMessage": "Login Timeout"}),
        ]
        try:
            yield _Vrli401RetryBundle(
                connector_instance=instance,
                db_target=target,
                session_route=session_route,
                version_route=version_route,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_OP_IDS: tuple[str, ...] = VRLI_CANARY_CORE_OP_IDS
assert len(_OP_IDS) == 6, (
    f"Expected 6 ingested vRLI browse-breadth ops after #2295 (events query is now the "
    f"vrli.event.query typed op), got {len(_OP_IDS)}: {_OP_IDS}"
)


@pytest.mark.parametrize("op_id", _OP_IDS, ids=lambda op: op)
async def test_vrli_e2e_all_ops_dispatch_ok(
    op_id: str,
    vrli_e2e_canary: _VrliE2EBundle,
) -> None:
    """All 6 curated ingested vRLI ops dispatch through the full dispatcher OK.

    The first call in the series fires the session-establish POST; subsequent
    calls reuse the cached token. The parametrise reports one CI case per
    op_id for granular failure attribution. The ingested ``aggregated-events``
    ``{constraints}`` op passes an empty-string constraints value to exercise
    the path-template substitution + empty-trailing-segment behaviour. The
    typed ``vrli.event.query`` op is covered separately (its own dispatch-level
    test module + the JSONFlux handle test below).
    """
    params = VRLI_CONSTRAINT_OP_PARAMS.get(op_id, {})
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VRLI_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": vrli_e2e_canary.target_name},
            "params": params,
        },
    )
    assert result["status"] == "ok", (
        f"vRLI op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


async def test_vrli_e2e_session_establishes_on_first_dispatch(
    vrli_e2e_canary: _VrliE2EBundle,
) -> None:
    """First dispatch to a fresh target fires POST /api/v2/sessions.

    Verifies the session-establish half of acceptance criterion (b) by
    inspecting the connector's session token cache before and after the
    first ``call_operation`` call.
    """
    instance = vrli_e2e_canary.connector_instance
    target_name = vrli_e2e_canary.target_name
    # The session-token cache is keyed on the tenant-unique (tenant_id, id)
    # tuple (#1642), not the bare name.
    cache_key = target_cache_key(vrli_e2e_canary.db_target)

    assert cache_key not in instance._session_tokens, (
        "Expected empty token cache before first dispatch; "
        f"got _session_tokens={instance._session_tokens!r}"
    )

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VRLI_CONNECTOR_ID,
            "op_id": "GET:/api/v2/version",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    assert instance._session_tokens.get(cache_key) == VRLI_CANARY_SESSION_ID, (
        f"Expected vRLI session token cached after first dispatch; "
        f"got _session_tokens={instance._session_tokens!r}"
    )


async def test_vrli_natural_product_token_dispatches_through_connector_not_shim(
    vrli_e2e_canary: _VrliE2EBundle,
) -> None:
    """A target with the literal ``product="vrli"`` token dispatches through VcfLogsConnector.

    The G0.26-T4 (#1798) SEV-2 regression. ``"vrli"`` is the natural
    operator token — what ``meho connector list`` emits (the
    parser-derived product) and what an operator types into ``POST
    /api/v1/targets`` — independent of any connector class attribute.
    Before the realignment this token resolved the auto-registered
    ``GenericRestConnector`` shim (``auth_headers`` →
    ``NotImplementedError`` → ``connector_unsupported`` /
    ``unreplaced_auto_shim``), never the hand-coded connector under the
    divergent ``vcf-logs`` namespace. After the alignment it resolves
    :class:`VcfLogsConnector`, so ``auth_headers`` runs (the vRLI session
    establishes) and the op returns ``status="ok"``.

    Seeds its own target with the **literal** string ``"vrli"`` (not
    ``VcfLogsConnector.product``) so the assertion pins the operator-token
    contract even if the class attribute were ever changed.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Target(
                tenant_id=VRLI_CANARY_OPERATOR_TENANT,
                name="vrli-natural-token-target",
                aliases=[],
                product="vrli",  # the literal natural operator token
                host=VRLI_CANARY_BASE_URL.removeprefix("https://"),
                port=443,
                fqdn=None,
                secret_ref="vrli/vrli-natural",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint=VRLI_CANARY_FINGERPRINT,
                notes="seeded by test_vrli_natural_product_token_dispatch",
            )
        )
        await session.commit()

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VRLI_CONNECTOR_ID,
            "op_id": "GET:/api/v2/version",
            "target": {"name": "vrli-natural-token-target"},
            "params": {},
        },
    )

    # Dispatched through VcfLogsConnector (auth_headers ran, session
    # established) — NOT the auto-shim, which would have returned
    # connector_unsupported / unreplaced_auto_shim.
    assert result["status"] == "ok", (
        f"a product='vrli' target must dispatch through VcfLogsConnector "
        f"(auth_headers runs), not the auto-shim; got error="
        f"{result.get('error')!r} full={result!r}"
    )
    assert result.get("error") != "connector_unsupported"


async def test_vrli_e2e_401_recovery_via_connector_method(
    vrli_e2e_401_recovery: _Vrli401RetryBundle,
) -> None:
    """A 401 on a downstream GET triggers one re-login + one retry — load-bearing for vRLI.

    The **key load-bearing E2E** for vRLI's session-token flavour
    (per Issue #838 body). Exercises:

    * Session-establish POST fires (initial cache hit).
    * Downstream GET returns 401.
    * Connector invalidates the cached session token.
    * Connector re-POSTs to ``/api/v2/sessions`` and receives a fresh
      token (distinct id, so we can assert cache turnover).
    * Connector retries the downstream GET and succeeds on the second
      attempt.

    Drives :meth:`VcfLogsConnector._get_json_with_session_retry`
    directly. The dispatch-ingested path calls ``_request_json``
    (no 401-retry at that level; the dispatcher converts 401 to
    ``connector_error``). The retry contract lives on the connector
    method, which is the call site for ``fingerprint()`` / ``probe()``
    and any future typed ops.

    Assertions:
    * The call returns the version JSON successfully (retry succeeded).
    * ``POST /api/v2/sessions`` called exactly twice (initial + post-401).
    * ``GET /api/v2/version`` called exactly twice (401 + retry).
    * Post-retry token cache holds the refreshed id, not the stale one.
    """
    bundle = vrli_e2e_401_recovery

    result = await bundle.connector_instance._get_json_with_session_retry(
        bundle.db_target,
        "/api/v2/version",
        operator=_OPERATOR,
    )

    assert result.get("version") == "9.0.0", (
        f"Expected version='9.0.0' in retry result; got {result!r}"
    )
    assert bundle.session_route.call_count == 2, (
        f"Expected session-create called twice (initial + post-401 relogin); "
        f"got call_count={bundle.session_route.call_count}"
    )
    assert bundle.version_route.call_count == 2, (
        f"Expected GET /api/v2/version called twice (401 + retry); "
        f"got call_count={bundle.version_route.call_count}"
    )
    cached = bundle.connector_instance._session_tokens.get(target_cache_key(bundle.db_target))
    assert cached == VRLI_CANARY_SESSION_REFRESH_ID, (
        f"Expected post-retry token to be the refreshed id {VRLI_CANARY_SESSION_REFRESH_ID!r}; "
        f"got cached token {cached!r}"
    )


async def test_vrli_e2e_second_401_fails_with_runtime_error(
    vrli_e2e_401_persists: _Vrli401RetryBundle,
) -> None:
    """If the post-relogin retry also 401s, RuntimeError naming the target is raised.

    The failure half of acceptance criterion (b): a session token that
    consistently 401s should fail fast rather than hammering vRLI's
    session-create endpoint in a loop. Mirrors the NSX precedent's
    posture verbatim.

    Asserts:
    * The connector raises ``RuntimeError`` (not ``SessionLoginError``
      — that one is reserved for failures *of* the session-login POST
      itself).
    * The error message names the target.
    * The error message references "401".
    * Session-create called exactly twice (no third re-login attempt).
    * Downstream GET called exactly twice (no third request).
    """
    bundle = vrli_e2e_401_persists

    with pytest.raises(RuntimeError) as exc_info:
        await bundle.connector_instance._get_json_with_session_retry(
            bundle.db_target,
            "/api/v2/version",
            operator=_OPERATOR,
        )

    msg = str(exc_info.value)
    assert _E2E_TARGET_NAME in msg, f"Expected target name in error; got {msg!r}"
    assert "401" in msg, f"Expected '401' in error; got {msg!r}"
    assert bundle.session_route.call_count == 2, (
        f"Expected session-create called twice (no third re-login attempt); "
        f"got call_count={bundle.session_route.call_count}"
    )
    assert bundle.version_route.call_count == 2, (
        f"Expected GET /api/v2/version called twice (401 + retry-401); "
        f"got call_count={bundle.version_route.call_count}"
    )


async def test_vrli_e2e_440_recovery_via_connector_method(
    vrli_e2e_440_recovery: _Vrli401RetryBundle,
) -> None:
    """A 440 (session expired) triggers one re-login + one retry — the #1909 fix.

    vRLI's ``trait.authenticated.440`` (*"the session ID has expired;
    obtain a new session ID"*) is the status the appliance emits once it
    idle-times out the in-memory session — observed live on v0.17.0 as a
    200 on the first call then 440 on every later call until restart.
    Before #1909 the connector keyed re-login strictly on 401 and
    re-raised 440 unretried, so the session never recovered. This asserts
    a 440 now drives the same invalidate -> re-login -> retry-once path
    as a 401.

    Assertions (identical to the 401-recovery contract):
    * The call returns the version JSON successfully (retry succeeded).
    * ``POST /api/v2/sessions`` called exactly twice (initial + post-440).
    * ``GET /api/v2/version`` called exactly twice (440 + retry).
    * Post-retry token cache holds the refreshed id, not the stale one.
    """
    bundle = vrli_e2e_440_recovery

    result = await bundle.connector_instance._get_json_with_session_retry(
        bundle.db_target,
        "/api/v2/version",
        operator=_OPERATOR,
    )

    assert result.get("version") == "9.0.0", (
        f"Expected version='9.0.0' in retry result; got {result!r}"
    )
    assert bundle.session_route.call_count == 2, (
        f"Expected session-create called twice (initial + post-440 relogin); "
        f"got call_count={bundle.session_route.call_count}"
    )
    assert bundle.version_route.call_count == 2, (
        f"Expected GET /api/v2/version called twice (440 + retry); "
        f"got call_count={bundle.version_route.call_count}"
    )
    cached = bundle.connector_instance._session_tokens.get(target_cache_key(bundle.db_target))
    assert cached == VRLI_CANARY_SESSION_REFRESH_ID, (
        f"Expected post-retry token to be the refreshed id {VRLI_CANARY_SESSION_REFRESH_ID!r}; "
        f"got cached token {cached!r}"
    )


async def test_vrli_e2e_second_440_fails_with_runtime_error(
    vrli_e2e_440_persists: _Vrli401RetryBundle,
) -> None:
    """If the post-relogin retry also 440s, RuntimeError naming the target is raised.

    The failure half of the 440 contract: a session that consistently
    440s should fail fast rather than looping. Mirrors the second-401
    posture verbatim, but the error names the 440 status.

    Asserts:
    * The connector raises ``RuntimeError``.
    * The error message names the target.
    * The error message references "440".
    * Session-create called exactly twice (no third re-login attempt).
    * Downstream GET called exactly twice (no third request).
    """
    bundle = vrli_e2e_440_persists

    with pytest.raises(RuntimeError) as exc_info:
        await bundle.connector_instance._get_json_with_session_retry(
            bundle.db_target,
            "/api/v2/version",
            operator=_OPERATOR,
        )

    msg = str(exc_info.value)
    assert _E2E_TARGET_NAME in msg, f"Expected target name in error; got {msg!r}"
    assert "440" in msg, f"Expected '440' in error; got {msg!r}"
    assert bundle.session_route.call_count == 2, (
        f"Expected session-create called twice (no third re-login attempt); "
        f"got call_count={bundle.session_route.call_count}"
    )
    assert bundle.version_route.call_count == 2, (
        f"Expected GET /api/v2/version called twice (440 + retry-440); "
        f"got call_count={bundle.version_route.call_count}"
    )


async def test_vrli_e2e_dispatch_writes_audit_row(
    vrli_e2e_canary: _VrliE2EBundle,
) -> None:
    """Each dispatch inserts an AuditLog row with method='DISPATCH', target_id, params_hash.

    Exercises acceptance criterion (c) — the issue's "All enabled ops
    write an audit row carrying op_id + target_id + params_hash" check.
    """
    op_id = "GET:/api/v2/version"
    sessionmaker = get_sessionmaker()

    async def _count_dispatch_rows() -> int:
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "DISPATCH",
                    AuditLog.path == op_id,
                )
            )
            return len(list(result.scalars().all()))

    baseline = await _count_dispatch_rows()

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VRLI_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": vrli_e2e_canary.target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"

    final = await _count_dispatch_rows()
    assert final - baseline == 1, (
        f"Expected exactly one new DISPATCH row for {op_id!r}; baseline={baseline} final={final}"
    )

    async with sessionmaker() as session:
        row_result = await session.execute(
            select(AuditLog)
            .where(
                AuditLog.method == "DISPATCH",
                AuditLog.path == op_id,
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        row = row_result.scalars().first()

    assert row is not None
    assert row.target_id is not None, (
        "AuditLog.target_id must not be None for a targeted dispatch; "
        f"got target_id=None on row {row!r}"
    )
    assert row.payload.get("op_id") == op_id, (
        f"AuditLog.payload['op_id'] must equal the dispatched op_id; got payload={row.payload!r}"
    )
    assert row.payload.get("params_hash"), (
        f"AuditLog.payload must carry a non-empty 'params_hash'; got payload={row.payload!r}"
    )


async def test_vrli_e2e_jsonflux_handle_populated_for_event_query(
    vrli_e2e_canary: _VrliE2EBundle,
) -> None:
    """Typed event query dispatched with the real JsonFluxReducer returns a populated handle.

    Exercises acceptance criterion (d) — the issue body's "vcf-logs
    query E2E asserts the JSONFlux handle path (handle →
    ``result_query`` drills in)". Since #2295 ``vrli.event.query`` is a
    ``source_kind="typed"`` op, so this also pins that the dispatcher's
    connector-agnostic reducer wraps a typed handler's ``{events: [...]}``
    envelope into a handle exactly as it did the ingested row's response —
    large result sets are precisely where the handle path matters.
    """
    events_payload = VRLI_CANARY_EVENTS["events"]
    assert isinstance(events_payload, list)
    expected_rows = len(events_payload)

    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        result_envelope = await call_operation(
            _OPERATOR,
            {
                "connector_id": VRLI_CONNECTOR_ID,
                "op_id": VRLI_FORCE_HANDLE_LIST_OP_ID,
                "target": {"name": vrli_e2e_canary.target_name},
                "params": {"constraints": ""},
            },
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result_envelope["status"] == "ok", (
        f"Expected JSONFlux dispatch to succeed; got {result_envelope!r}"
    )

    handle = result_envelope.get("handle")
    assert handle is not None, (
        "Expected OperationResult.handle to be populated by JsonFluxReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )

    uuid.UUID(handle["handle_id"])

    assert handle["total_rows"] == expected_rows, (
        f"Expected {expected_rows} event rows from VRLI_CANARY_EVENTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )

    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"Expected ≥1 sample row from the seeded vRLI event list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"Expected reducer summary on result.row_count={expected_rows}; got result={payload!r}"
    )


# ---------------------------------------------------------------------------
# Reserved-expansion constraint canary (#2003)
# ---------------------------------------------------------------------------


@pytest.fixture
async def vrli_e2e_reserved_constraint(
    captured_events: list[Any],
) -> AsyncIterator[tuple[_VrliE2EBundle, Any]]:
    """vRLI setup seeding the ``{+constraints}`` reserved-expansion events op.

    Layers a single non-curated ``GET:/api/v2/events/{+constraints}``
    descriptor on top of the standard happy-path setup and registers a
    respx route against the **literal-slash** wire path. The route is
    yielded so the test can assert it was hit — a ``%2F``-mangled URL
    would miss it.
    """
    del captured_events

    await _insert_vrli_descriptors()
    await _insert_vrli_reserved_constraint_descriptor()
    seeded_target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vrli_routes(mock)
        reserved_route = _register_vrli_reserved_constraint_route(mock)
        try:
            yield (
                _VrliE2EBundle(
                    target_name=_E2E_TARGET_NAME,
                    connector_instance=instance,
                    db_target=seeded_target,
                ),
                reserved_route,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


async def test_vrli_e2e_reserved_constraint_keeps_slash_literal_on_wire(
    vrli_e2e_reserved_constraint: tuple[_VrliE2EBundle, Any],
) -> None:
    """A non-empty ``{+constraints}`` query keeps ``/`` literal on the wire.

    The constraint-query gap the empty-constraint canary cannot exercise:
    a reserved-char constraint (``text/CONTAINS error/...``) dispatched
    through the full ``call_operation`` stack must reach vRLI with the
    slash-delimited chain intact (``%2F``-mangling 400s the appliance).
    Asserts the dispatch succeeds AND the literal-slash respx route was
    the one hit — proof the wire URL was not over-encoded (#2003).
    """
    bundle, reserved_route = vrli_e2e_reserved_constraint

    result_envelope = await call_operation(
        _OPERATOR,
        {
            "connector_id": VRLI_CONNECTOR_ID,
            "op_id": VRLI_RESERVED_CONSTRAINT_OP_ID,
            "target": {"name": bundle.target_name},
            "params": {"constraints": VRLI_RESERVED_CONSTRAINT_VALUE},
        },
    )

    assert result_envelope["status"] == "ok", (
        f"reserved-constraint dispatch did not succeed: {result_envelope!r}"
    )
    assert reserved_route.called, (
        "literal-slash wire route was never hit — the constraint chain was "
        "over-encoded (%2F) instead of passing through as reserved expansion"
    )
    # The wire path on the actual request keeps the structural slashes.
    wire_path = reserved_route.calls.last.request.url.path
    assert wire_path == "/api/v2/events/text/CONTAINS error/hostname/CONTAINS vcsa", (
        f"unexpected wire path: {wire_path!r}"
    )
