# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Readiness-pill tests for the operator-console chassis (G10.7-T1, #1776).

The sidebar-footer readiness pill in ``base.html`` colours ``bg-success``
/ "ready" vs ``bg-warning`` / "starting" off the ``ready`` template
variable. Before #1776 only the dashboard computed it; every other
``/ui/*`` surface hardcoded ``ready=False`` in its own context dict, so
the pill was stuck on yellow "starting" on every page but the dashboard
regardless of actual backend health.

The fix injects the live verdict into *every* render from
``request.state.ui_ready`` -- a short-TTL-cached
:func:`~meho_backplane.health.readiness_snapshot` computed once per
request by :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`
and read by the synchronous chassis context processor
(:func:`~meho_backplane.ui.templating._ui_session_context_processor`).

This suite covers three layers:

* The cached snapshot helper itself -- fail-closed empty registry, the
  TTL window, and ``max_age_s=0`` force-fresh (the dashboard's path).
* The context processor's injection + fail-safe default.
* The acceptance criterion end-to-end: rendering a **non-dashboard**
  surface (``GET /ui/memory``) through the full TestClient chassis shows
  green "ready" when the registered probes pass and yellow "starting"
  when a probe fails -- proving the pill reflects readiness rather than a
  hardcoded ``False`` -- while the dashboard's behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.requests import Request

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.health import (
    ProbeResult,
    clear_probes,
    clear_readiness_cache,
    readiness_snapshot,
    register_probe,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import (
    _ui_session_context_processor,
    reset_templating_for_testing,
)
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP_A = "op-alice"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars; mirrors the other UI surface suites."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _isolated_readiness() -> Iterator[None]:
    """Reset the probe registry + cached snapshot around every test.

    Both are process globals; without this a probe set or a cached
    verdict from one test leaks into the next on the same xdist worker
    (run-order becomes load-bearing). Clear before *and* after so a
    mid-test abort can't poison a sibling.
    """
    clear_probes()
    clear_readiness_cache()
    yield
    clear_probes()
    clear_readiness_cache()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app with the full UI chassis wired in.

    Mirrors the production wiring + the other surface suites: StaticFiles
    at ``/ui/static``, BFF auth router + UI surface router,
    ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
    """
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert one ``tenant`` row so the session's tenant FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row directly and return its UUID."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _jwks() -> dict[str, Any]:
    """Mint a JWKS document for the discovery/JWKS respx stub.

    The ``GET /ui/memory`` list render only needs the session cookie
    (``require_ui_session``); no token is presented. The stub keeps the
    auth router happy if any transitive lookup touches discovery.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-readiness-test-kid")
    return _public_jwks(keypair)


def _authenticated_client(session_id: uuid.UUID) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient with the session cookie + an open respx mock."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, _jwks())
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


def _pill_state(html: str) -> tuple[str | None, str | None]:
    """Extract the footer pill's ``(colour-class, label)`` from rendered HTML.

    ``base.html`` renders the dot as
    ``rounded-full {bg-success|bg-warning}`` and the label as a
    ``<span>{ready|starting}</span>``. Returning both lets a test assert
    the pill reflects readiness end-to-end rather than trusting a single
    substring that could appear elsewhere on the page.
    """
    colour = re.search(r"rounded-full\s+(bg-success|bg-warning)", html)
    label = re.search(r"<span>(ready|starting)</span>", html)
    return (colour.group(1) if colour else None, label.group(1) if label else None)


def _make_request(state: dict[str, Any]) -> Request:
    """Build a bare ``/ui/*`` GET request carrying *state* on its scope."""
    return Request(
        {"type": "http", "method": "GET", "path": "/ui/memory", "headers": [], "state": state}
    )


# ---------------------------------------------------------------------------
# Cached readiness snapshot helper
# ---------------------------------------------------------------------------


async def test_snapshot_empty_registry_is_not_ready() -> None:
    """An empty probe registry fails closed (``all([])`` is vacuously True)."""
    snapshot = await readiness_snapshot(max_age_s=0)
    assert snapshot == {"ready": False, "checks": []}


async def test_snapshot_all_passing_is_ready_with_checks() -> None:
    """Every probe ``ok`` -> ready, and the checks detail mirrors ``/ready``."""
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True, detail="auth ok"))
    register_probe("db", lambda: ProbeResult(name="db", ok=True))
    snapshot = await readiness_snapshot(max_age_s=0)
    assert snapshot["ready"] is True
    assert snapshot["checks"] == [
        {"name": "vault", "ok": True, "detail": "auth ok"},
        {"name": "db", "ok": True, "detail": ""},
    ]


async def test_snapshot_one_failing_probe_is_not_ready() -> None:
    """A single failing probe flips the verdict to not-ready."""
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe("db", lambda: ProbeResult(name="db", ok=False, detail="migration pending"))
    snapshot = await readiness_snapshot(max_age_s=0)
    assert snapshot["ready"] is False


async def test_snapshot_timeout_degrades_to_not_ready_without_hanging() -> None:
    """A probe slower than ``timeout_s`` degrades to not-ready, promptly.

    The registry's real probes do live network I/O with no internal
    timeout, so a blocked dependency would hang the per-request sweep
    indefinitely (#1776 CI-unit-lane hang). ``timeout_s`` bounds it: the
    call must return well inside the bound's slack with ``ready=False``
    and a synthetic ``timeout`` check -- not raise, not block for the
    probe's full sleep.
    """

    async def _slow_probe() -> ProbeResult:
        await asyncio.sleep(5)  # >> the 0.1s bound below
        return ProbeResult(name="slow", ok=True)

    register_probe("slow", _slow_probe)
    loop = asyncio.get_running_loop()
    started = loop.time()
    snapshot = await readiness_snapshot(max_age_s=0, timeout_s=0.1)
    elapsed = loop.time() - started

    assert snapshot["ready"] is False
    assert snapshot["checks"] == [
        {"name": "timeout", "ok": False, "detail": "readiness probe sweep exceeded 0.1s"}
    ]
    # Returned on the timeout, not after the probe's 5s sleep.
    assert elapsed < 2.0, f"sweep should bail at the bound, took {elapsed:.2f}s"


async def test_snapshot_timeout_is_not_cached() -> None:
    """A timed-out sweep must not pin a misleading verdict for the TTL.

    Caching the not-ready timeout result would lock the pill to
    "starting" for the whole window even after the dependency recovers.
    The next caller must re-run the sweep; here a fast probe registered
    after the timeout is seen immediately on a cached (``max_age_s>0``)
    read, proving nothing was cached by the timed-out call.
    """

    async def _slow_probe() -> ProbeResult:
        await asyncio.sleep(5)
        return ProbeResult(name="slow", ok=True)

    register_probe("slow", _slow_probe)
    timed_out = await readiness_snapshot(max_age_s=60, timeout_s=0.1)
    assert timed_out["ready"] is False

    clear_probes()
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    # max_age_s=60 would serve a cached verdict had the timeout cached one;
    # instead it runs a fresh sweep and sees the healthy probe.
    after = await readiness_snapshot(max_age_s=60)
    assert after["ready"] is True, "timed-out verdict must not have been cached"


async def test_snapshot_unbounded_default_preserves_dashboard_path() -> None:
    """``timeout_s=None`` (default) leaves the sweep unbounded.

    ``GET /ready`` and the dashboard (``max_age_s=0``) rely on the
    unbounded sweep; this guards that a fast probe set still produces a
    cached verdict identical to the pre-#1776 behaviour when no bound is
    passed.
    """
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe("db", lambda: ProbeResult(name="db", ok=True))
    snapshot = await readiness_snapshot(max_age_s=0)
    assert snapshot["ready"] is True
    assert [c["name"] for c in snapshot["checks"]] == ["vault", "db"]  # type: ignore[index,union-attr]


async def test_snapshot_serves_cached_verdict_within_ttl() -> None:
    """A cached verdict younger than the window is served without re-probing.

    Registering a *failing* probe after a healthy snapshot is cached must
    not change the cached read (the cache is the whole point of the hot
    path), while ``max_age_s=0`` forces a fresh sweep that sees it.
    """
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    first = await readiness_snapshot(max_age_s=60)
    assert first["ready"] is True

    register_probe("db", lambda: ProbeResult(name="db", ok=False))
    cached = await readiness_snapshot(max_age_s=60)
    assert cached["ready"] is True, "stale-but-fresh-enough cache must be served"

    fresh = await readiness_snapshot(max_age_s=0)
    assert fresh["ready"] is False, "max_age_s=0 must bypass the cache"


# ---------------------------------------------------------------------------
# Context processor injection
# ---------------------------------------------------------------------------


def test_processor_injects_ready_true_from_state() -> None:
    """``request.state.ui_ready=True`` surfaces as ``ready=True``."""
    ctx = _ui_session_context_processor(_make_request({"ui_ready": True}))
    assert ctx["ready"] is True


def test_processor_injects_ready_false_from_state() -> None:
    """``request.state.ui_ready=False`` surfaces as ``ready=False``."""
    ctx = _ui_session_context_processor(_make_request({"ui_ready": False}))
    assert ctx["ready"] is False


def test_processor_defaults_to_starting_when_unbound() -> None:
    """No bound verdict (auth/static surfaces) fails safe to ``ready=False``.

    ``base.html`` reads ``ready`` under ``StrictUndefined``, so the key
    must always be present even when the middleware never ran.
    """
    ctx = _ui_session_context_processor(_make_request({}))
    assert ctx["ready"] is False
    assert ctx["session_tenant"] is None


# ---------------------------------------------------------------------------
# Acceptance criterion: the pill reflects readiness on a NON-dashboard route
# ---------------------------------------------------------------------------


def test_non_dashboard_pill_is_ready_when_backend_healthy() -> None:
    """``GET /ui/memory`` shows green "ready" when the readiness probes pass.

    The memory surface used to hardcode ``ready=False``; this asserts the
    pill now reflects the live verdict (#1776). A healthy backend == every
    registered probe ``ok``, the same condition ``GET /ready`` returns 200
    on.
    """
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe("db", lambda: ProbeResult(name="db", ok=True))
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    # Sanity: this is the memory surface, not the dashboard.
    assert "<title>Memory" in response.text
    assert _pill_state(response.text) == ("bg-success", "ready")


def test_non_dashboard_pill_is_starting_when_backend_not_ready() -> None:
    """``GET /ui/memory`` shows yellow "starting" when ``/ready`` would 503.

    A failing probe is the not-ready condition (``/ready`` returns 503);
    the pill must follow it on a non-dashboard surface rather than being
    pinned to a hardcoded value.
    """
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe("db", lambda: ProbeResult(name="db", ok=False, detail="migration pending"))
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "<title>Memory" in response.text
    assert _pill_state(response.text) == ("bg-warning", "starting")


def test_non_dashboard_pill_is_starting_with_empty_registry() -> None:
    """An empty probe registry (fail-closed) renders "starting", not "ready".

    Guards the vacuous-``all([])`` trap end-to-end through the render
    path: zero probes must not flip the pill green.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert _pill_state(response.text) == ("bg-warning", "starting")


def test_non_dashboard_pill_is_starting_when_probe_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung probe degrades the render to "starting" instead of hanging it.

    Regression for the #1776 CI-unit-lane hang: moving the readiness
    sweep into the per-request middleware made every ``/ui/*`` render
    block on a slow/black-holed probe (no internal timeout on the real
    Keycloak/Vault/DB probes). The middleware now bounds the sweep, so a
    probe that sleeps far longer than the bound must still let the page
    render **promptly** with the fail-safe "starting" pill rather than
    starving the request. The bound is shrunk here so the assertion runs
    fast.
    """
    from meho_backplane.ui.auth import middleware as ui_middleware

    monkeypatch.setattr(ui_middleware, "_READINESS_TIMEOUT_S", 0.1)

    async def _hung_probe() -> ProbeResult:
        await asyncio.sleep(10)  # >> the 0.1s bound; would hang the render pre-fix
        return ProbeResult(name="keycloak", ok=True)

    register_probe("keycloak", _hung_probe)
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id)
    loop_start = time.monotonic()
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    elapsed = time.monotonic() - loop_start

    assert response.status_code == 200, response.text
    assert "<title>Memory" in response.text
    assert _pill_state(response.text) == ("bg-warning", "starting")
    # The render returned on the readiness bound, not the probe's 10s sleep.
    assert elapsed < 5.0, f"render should not block on the hung probe, took {elapsed:.2f}s"


def test_dashboard_pill_follows_probe_state_unchanged() -> None:
    """The dashboard pill still reflects a fresh probe sweep (behaviour unchanged).

    The dashboard computes its own verdict and writes it to
    ``request.state.ui_ready`` so the context processor re-injects the
    same value; the footer pill and the dashboard's readiness card stay
    in lock-step. This asserts the dashboard remains correct after the
    context-processor change.
    """
    register_probe("vault", lambda: ProbeResult(name="vault", ok=True))
    register_probe("db", lambda: ProbeResult(name="db", ok=True))
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id)
    try:
        response = client.get("/ui/")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert _pill_state(response.text) == ("bg-success", "ready")
