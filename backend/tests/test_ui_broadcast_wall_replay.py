# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast wall-monitor + Last-24h replay.

Initiative #338 (G10.1 Activity broadcast UI), Task #869 (G10.1-T3).
Acceptance criteria on issue #869:

* ``GET /ui/broadcast?wall=1`` hides the sidebar + top bar, maximises the
  feed, auto-scrolls.
* Wall mode survives an access-token expiry without logging out (the
  server-side sliding-session extension in ``load_session``) -- asserted
  across a forced near-expiry.
* The "Last 24h" replay tab reads the tenant's stored agent
  announcements of the last 24 hours from the archive (#3758) and renders
  them with the same row partial.
* Cross-tenant isolation holds in wall + replay (tenant-A events absent
  from tenant-B).
* ``ruff`` + ``mypy`` clean; ``pytest -n auto`` passes.

Three test surfaces:

* **Wall page** -- HTTP edge: a minimal app wired with the UI session +
  CSRF middlewares; asserts the wall layout drops the base.html chrome
  and embeds the same feed fragment.
* **History fragment** -- HTTP edge over seeded archive rows: the
  rendered rows, the empty state, the row cap, cross-tenant isolation,
  and markup breakout.
* **Sliding-session extension** -- drives ``load_session`` directly: a
  near-expiry session is extended on an active load; an at-cap session
  is not; ``sliding=0`` disables the extension.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import (
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    load_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.broadcast.feed import IN_DOM_ROW_CAP
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.broadcast_admission_helpers import seed_announcement
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + broadcast env vars for every test.

    Mirrors :func:`backend.tests.test_ui_broadcast_feed._bff_env`. Cache
    + global-state resets run on setup and teardown so a failing test
    cannot leak templating / session-engine / broadcast-client state
    into the next case.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the broadcast UI tests."""
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


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
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


async def _seed_session_async(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Async counterpart of :func:`_seed_session_sync` for ``async def`` tests.

    The sync helper wraps :func:`asyncio.run`, which cannot be called
    from inside a running event loop (the ``asyncio_mode = "auto"`` test
    methods already run on one). The sliding-session tests drive
    ``load_session`` with ``await``, so they seed via this helper
    instead.
    """
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


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Wall-monitor mode (work item #5)
# ---------------------------------------------------------------------------


class TestWallMode:
    """``GET /ui/broadcast?wall=1`` -- the no-chrome wall view."""

    def test_wall_view_drops_base_chrome(self) -> None:
        """The wall layout hides the sidebar + top navbar + filter bar."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast?wall=1")
        assert response.status_code == 200, response.text
        body = response.text
        # The wall page is a standalone document, not the base.html shell:
        # no DaisyUI drawer chrome, no sidebar "Surfaces" nav, no filter bar.
        assert "drawer-toggle" not in body
        assert "Surfaces" not in body
        assert "Filter broadcast events" not in body
        # It is its own <title>, distinct from the in-chrome view.
        assert "Broadcast wall" in body

    def test_wall_view_maximises_and_autoscrolls_the_feed(self) -> None:
        """The wall view embeds the feed fragment in auto-scroll mode."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast?wall=1")
        body = response.text
        # The same SSE bridge wiring as the in-chrome feed (reused fragment).
        assert 'sse-connect="/ui/broadcast/stream"' in body
        assert 'sse-swap="broadcast"' in body
        # Wall mode flips the controller's auto-scroll on + makes the list
        # scrollable (the feed fragment forwards ``wall=True``).
        assert "autoScroll: true" in body
        assert 'x-ref="list"' in body
        # The shared controller script is loaded.
        assert "/ui/static/src/app/broadcast-feed.js" in body

    def test_wall_view_loads_controller_before_alpine(self) -> None:
        """The controller script tag precedes the Alpine bundle (#1692).

        The wall page is a standalone document, so it places the
        controller ``<script>`` in its own head, before the
        ``_head_assets.html`` include that loads ``alpine.min.js`` --
        the same document-order contract base.html's
        ``component_scripts`` block gives the in-chrome view. Deferred
        scripts execute in document order, and the Alpine CDN bundle
        fires ``alpine:init`` from a microtask at the end of its own
        script task; a body-end controller registers too late and the
        wall renders permanently empty with no auto-scroll.
        """
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast?wall=1")
        body = response.text
        controller_pos = body.index("/ui/static/src/app/broadcast-feed.js")
        alpine_pos = body.index("/ui/static/src/vendor/alpine.min.js")
        assert controller_pos < alpine_pos

    def test_in_chrome_view_keeps_chrome_and_no_autoscroll(self) -> None:
        """The default (non-wall) view still renders base.html chrome."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast")
        body = response.text
        # Base chrome present; auto-scroll off (the default feed density).
        assert "Surfaces" in body
        assert "autoScroll: false" in body
        # The in-chrome view links to the wall view + offers the Last-24h tab.
        assert "/ui/broadcast?wall=1" in body
        assert "Last 24h" in body

    def test_wall_view_unauthenticated_redirects_to_login(self) -> None:
        """``?wall=1`` is still gated by the session middleware."""
        with respx.mock(assert_all_called=False):
            client = TestClient(_build_app(), follow_redirects=False)
            response = client.get("/ui/broadcast?wall=1")
        assert response.status_code == 302
        assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Long-display session refresh -- sliding-session extension
# ---------------------------------------------------------------------------


class TestSlidingSessionExtension:
    """``load_session`` slides a near-expiry session forward (work item #5)."""

    async def _load(self, session_id: uuid.UUID) -> object:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            return await load_session(session, session_id)

    async def test_near_expiry_session_is_extended_on_active_load(self) -> None:
        """A session within the sliding window has its expiry pushed out.

        This is the long-display durability guarantee: a wall monitor's
        SSE reconnect is an active ``/ui/*`` load, so an about-to-expire
        session is kept alive rather than lapsing into a 302-to-login
        that would permanently kill the browser ``EventSource``.
        """
        # 90s lifetime, default sliding window 3600s -> the session is
        # already inside the window at creation, so the next load slides
        # ``expires_at`` out to ~now + 3600s.
        session_id = await _seed_session_async(tenant_id=_TENANT_A, lifetime=timedelta(seconds=90))
        before = datetime.now(UTC) + timedelta(seconds=90)
        decrypted = await self._load(session_id)
        assert decrypted is not None
        # Extended well past the original ~90s expiry.
        assert decrypted.expires_at > before + timedelta(seconds=60)

    async def test_extension_never_exceeds_absolute_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sliding extension is bounded by the absolute lifetime cap."""
        # Tiny absolute cap so a single slide hits the ceiling.
        monkeypatch.setenv("UI_SESSION_ABSOLUTE_LIFETIME_SECONDS", "120")
        monkeypatch.setenv("UI_SESSION_SLIDING_EXTENSION_SECONDS", "3600")
        get_settings.cache_clear()
        session_id = await _seed_session_async(tenant_id=_TENANT_A, lifetime=timedelta(seconds=30))
        decrypted = await self._load(session_id)
        assert decrypted is not None
        # created_at + 120s is the ceiling; the slide cannot pass it.
        ceiling = decrypted.created_at + timedelta(seconds=120)
        assert decrypted.expires_at <= ceiling + timedelta(seconds=1)

    async def test_sliding_disabled_leaves_expiry_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sliding=0`` disables the extension entirely."""
        monkeypatch.setenv("UI_SESSION_SLIDING_EXTENSION_SECONDS", "0")
        get_settings.cache_clear()
        session_id = await _seed_session_async(tenant_id=_TENANT_A, lifetime=timedelta(seconds=90))
        decrypted = await self._load(session_id)
        assert decrypted is not None
        # Within ~90s of now (clock skew margin), not pushed to +3600s.
        assert decrypted.expires_at < datetime.now(UTC) + timedelta(seconds=300)


# ---------------------------------------------------------------------------
# Last-24h replay pane (work item #6)
# ---------------------------------------------------------------------------


def _seed(tenant_id: uuid.UUID, **fields: Any) -> None:
    asyncio.run(seed_announcement(tenant_id, **fields))


class TestHistoryReplay:
    """``GET /ui/broadcast/history`` -- the archive-backed replay fragment (#3758)."""

    def test_history_renders_the_session_tenants_announcements(self) -> None:
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        _seed(_TENANT_A, activity="rolling the cluster")
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        body = response.text
        assert "rolling the cluster" in body
        assert "broadcastFeed(" in body
        assert "Last 24h replay" in body

    def test_history_empty_window_renders_empty_state(self) -> None:
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        assert "No activity in the last" in response.text

    def test_history_is_bounded_by_window_and_cap(self) -> None:
        """Rows older than the window are left out; at most the row cap renders."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        _seed(_TENANT_A, activity="too old", ts=datetime.now(UTC) - timedelta(days=3))
        for index in range(IN_DOM_ROW_CAP + 2):
            _seed(_TENANT_A, activity=f"row-{index:04d}")
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        body = response.text
        assert "too old" not in body
        assert "row-0001" not in body
        assert f"row-{IN_DOM_ROW_CAP + 1:04d}" in body

    def test_history_cross_tenant_isolation(self) -> None:
        """A tenant-B session never sees tenant A's announcements."""
        session_id = _seed_session_sync(tenant_id=_TENANT_B)
        _seed(_TENANT_A, activity="tenant a secret plan")
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        assert "tenant a secret plan" not in response.text

    def test_history_event_fields_cannot_break_out_of_markup(self) -> None:
        """A quote-/markup-bearing announcement field renders harmlessly (B1).

        Regression for PR #1044 B1: the pane emits events as inert
        ``<script type="application/json">`` text rendered through Jinja
        ``| tojson``. An announcement whose free-text fields contain
        ``"`` ``'`` ``<`` ``>`` ``&`` and a literal ``</script>`` must not
        break out of the data island, and must still round-trip into it.
        """
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        injection = (
            "</script><img src=x onerror=alert(1)>\" autofocus onfocus=alert(2) ' < > & done"
        )
        _seed(_TENANT_A, activity=injection, target=injection, principal_sub=injection)
        with respx.mock(assert_all_called=False):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        body = response.text
        assert "<img src=x onerror=" not in body
        assert "</script><img" not in body
        # The only raw "</script>" allowed is the data island's own closer.
        assert body.count("</script>") == 1
        assert 'id="broadcast-history-data"' in body
        assert "\\u003c/script\\u003e" in body
        assert "\\u003cimg" in body
        assert "seedFrom: 'broadcast-history-data'" in body

    def test_history_unauthenticated_redirects_to_login(self) -> None:
        """The history fragment is gated by the session middleware."""
        with respx.mock(assert_all_called=False):
            client = TestClient(_build_app(), follow_redirects=False)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 302
        assert response.headers["location"].startswith("/ui/auth/login?return_to=")

    def test_history_does_not_need_valkey(self) -> None:
        """The pane reads the archive; a Valkey outage does not affect it."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        _seed(_TENANT_A, activity="stored while valkey is down")
        broadcast_client = get_broadcast_client()
        mock = AsyncMock(side_effect=RedisConnectionError("simulated valkey teardown"))
        with (
            respx.mock(assert_all_called=False),
            patch.object(broadcast_client, "xrange", new=mock),
            patch.object(broadcast_client, "xrevrange", new=mock),
        ):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        assert "stored while valkey is down" in response.text
        assert mock.await_count == 0
