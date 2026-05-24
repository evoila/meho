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
* The "Last 24h" replay tab pulls the tenant's last-24h events (a finite
  ``XRANGE`` over ``meho:feed:{tenant}``) and renders historical rows
  that open the same T2 drawer.
* Cross-tenant isolation holds in wall + replay (tenant-A events absent
  from tenant-B).
* ``ruff`` + ``mypy`` clean; ``pytest -n auto`` passes.

Three test surfaces:

* **Wall page** -- HTTP edge: a minimal app wired with the UI session +
  CSRF middlewares; asserts the wall layout drops the base.html chrome
  and embeds the same feed fragment.
* **History fragment** -- HTTP edge with a mocked broadcast client's
  ``xrange``: asserts the tenant-scoped key, the rendered rows, the
  empty state, and cross-tenant isolation. ``xrange`` returns a finite
  list, so the endpoint terminates -- there is no streaming loop to
  hang.
* **Sliding-session extension** -- drives ``load_session`` directly: a
  near-expiry session is extended on an active load; an at-cap session
  is not; ``sliding=0`` disables the extension.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import (
    BroadcastEvent,
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


def _make_event(
    *,
    tenant_id: UUID = _TENANT_A,
    op_class: str = "read",
    principal_sub: str = "op-test",
    target_name: str | None = "rdc-vcenter",
    op_id: str = "vsphere.vm.list",
) -> BroadcastEvent:
    """Build a redacted-shape :class:`BroadcastEvent` for the replay tests."""
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime(2026, 5, 13, tzinfo=UTC),
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        target_name=target_name,
        op_id=op_id,
        op_class=op_class,
        result_status="ok",
        audit_id=UUID("33333333-3333-3333-3333-333333333333"),
        payload={"op_class": op_class, "params": {}, "result_status": "ok"},
    )


def _xrange_returning(events: list[BroadcastEvent]) -> AsyncMock:
    """AsyncMock standing in for ``Redis.xrange`` returning *events*.

    ``xrange`` is a **finite** read (not a BLOCKing stream loop), so a
    plain ``AsyncMock(return_value=...)`` is correct here: the history
    endpoint awaits it once, gets the list, and returns. There is no
    ``while True`` to spin, so the async-hang failure mode that haunts
    the stream tests (a bare ``return_value=None`` inside an XREAD loop)
    does not apply -- the endpoint terminates regardless.

    The returned shape mirrors redis-py's ``XRANGE`` reply: a list of
    ``(entry_id, fields_dict)`` tuples, ascending (oldest-first).
    """
    items = [
        (f"{1715600000000 + i}-0", {"event": event.model_dump_json()})
        for i, event in enumerate(events)
    ]
    return AsyncMock(return_value=items)


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


class TestHistoryReplay:
    """``GET /ui/broadcast/history`` -- the finite XRANGE replay fragment."""

    def test_history_renders_events_from_session_tenant_stream(self) -> None:
        """The pane pulls the session tenant's stream key via XRANGE."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        event = _make_event(tenant_id=_TENANT_A, op_id="vsphere.vm.list")
        mock = _xrange_returning([event])
        broadcast_client = get_broadcast_client()
        with (
            respx.mock(assert_all_called=False),
            patch.object(broadcast_client, "xrange", new=mock),
        ):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        # The XRANGE was issued against the session tenant's key.
        assert mock.await_count == 1
        assert mock.await_args.args[0] == f"meho:feed:{_TENANT_A}"
        # The event JSON is seeded into the shared controller; the row
        # partial + the 24h-window status copy render.
        body = response.text
        assert "vsphere.vm.list" in body
        assert "broadcastFeed(" in body
        assert "Last 24h replay" in body

    def test_history_empty_window_renders_empty_state(self) -> None:
        """No events in the 24h window renders the empty-state copy."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        mock = _xrange_returning([])
        broadcast_client = get_broadcast_client()
        with (
            respx.mock(assert_all_called=False),
            patch.object(broadcast_client, "xrange", new=mock),
        ):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        assert "No activity in the last" in response.text

    def test_history_xrange_is_bounded_by_window_and_cap(self) -> None:
        """XRANGE spans the 24h window to '+' and is COUNT-capped.

        Proves the pull is finite (a row cap, not an unbounded read), so
        the endpoint terminates -- there is no streaming loop here.
        """
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        mock = _xrange_returning([_make_event()])
        broadcast_client = get_broadcast_client()
        with (
            respx.mock(assert_all_called=False),
            patch.object(broadcast_client, "xrange", new=mock),
        ):
            client = _authenticated_client(session_id)
            client.get("/ui/broadcast/history")
        kwargs = mock.await_args.kwargs
        # End anchor is the live tail; count is the in-DOM row cap.
        assert kwargs["max"] == "+"
        assert kwargs["count"] == IN_DOM_ROW_CAP
        # Start id is a bare ms-timestamp (24h window), strictly positive.
        assert int(kwargs["min"]) > 0

    def test_history_cross_tenant_isolation(self) -> None:
        """A tenant-B session reads only tenant-B's stream, never A's."""
        session_id = _seed_session_sync(tenant_id=_TENANT_B)
        mock = _xrange_returning([])
        broadcast_client = get_broadcast_client()
        with (
            respx.mock(assert_all_called=False),
            patch.object(broadcast_client, "xrange", new=mock),
        ):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        key = mock.await_args.args[0]
        assert key == f"meho:feed:{_TENANT_B}"
        assert str(_TENANT_A) not in key

    def test_history_skips_malformed_entries(self) -> None:
        """A malformed stream entry is skipped, not 500'd."""
        session_id = _seed_session_sync(tenant_id=_TENANT_A)
        good = _make_event(op_id="vsphere.vm.list")
        # One good entry, one with a non-JSON event field, one with no
        # event field at all -- only the good one should render.
        items = [
            ("1715600000000-0", {"event": good.model_dump_json()}),
            ("1715600000001-0", {"event": "not-json{"}),
            ("1715600000002-0", {"other": "field"}),
        ]
        mock = AsyncMock(return_value=items)
        broadcast_client = get_broadcast_client()
        with (
            respx.mock(assert_all_called=False),
            patch.object(broadcast_client, "xrange", new=mock),
        ):
            client = _authenticated_client(session_id)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 200, response.text
        assert "vsphere.vm.list" in response.text

    def test_history_unauthenticated_redirects_to_login(self) -> None:
        """The history fragment is gated by the session middleware."""
        with respx.mock(assert_all_called=False):
            client = TestClient(_build_app(), follow_redirects=False)
            response = client.get("/ui/broadcast/history")
        assert response.status_code == 302
        assert response.headers["location"].startswith("/ui/auth/login?return_to=")
