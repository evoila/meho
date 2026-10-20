# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast live-feed UI surface.

Initiative #338 (G10.1 Activity broadcast UI), Task #867 (G10.1-T1).
The acceptance criteria on issue #867 are:

* ``GET /ui/broadcast`` renders the feed for the logged-in operator's
  tenant (extends ``base.html``; sidebar active-state on Broadcast).
* Live events render as server-side Jinja2 row fragments via the HTMX
  ``sse`` extension; ``op_class`` colour-coding via DaisyUI badges.
* Reconnect after a forced drop replays missed events via
  ``Last-Event-Id`` (the SSE bridge reuses the feed cursor resolver).
* In-DOM list capped at 1000 rows (Alpine trim); empty state renders
  when no events match.
* Cross-tenant isolation: tenant-A events never render on tenant-B's
  feed.
* ``ruff`` + ``mypy`` clean; ``pytest -n auto`` passes.

Two test surfaces:

* **HTTP edge** (``TestBroadcastFeedPage`` / ``TestBroadcastStreamAuth``)
  -- mirrors :mod:`backend.tests.test_ui_topology_table`: a minimal app
  wired with the UI session + CSRF middlewares, an authenticated client
  via a seeded ``web_session`` row, and the unauthenticated 302 check.
* **SSE bridge** (``TestBroadcastStreamBridge``) -- since #3758 the
  bridge hands the session tenant and the resolved start to the shared
  archive generator (:func:`announcement_feed_frames`, covered in
  :mod:`backend.tests.test_api_v1_feed`). These tests pin what the
  bridge passes in: the session tenant (cross-tenant isolation), the
  resume checkpoint, and the filters.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.api.v1.feed import FeedStart
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import (
    OP_CLASS_ENUM,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.archive_cursor import encode_watch_checkpoint
from meho_backplane.broadcast.archive_reader import AnnouncementFilter
from meho_backplane.db.engine import reset_engine_for_testing
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
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.broadcast import stream as stream_module
from meho_backplane.ui.routes.broadcast.feed import (
    IN_DOM_ROW_CAP,
    OP_CLASS_BADGE_CLASSES,
    OP_CLASS_FILTER_OPTIONS,
)
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

# Two stable tenant ids -- distinct values so the cross-tenant isolation
# assertion has concrete state. Same shape the topology + feed suites use.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + broadcast env vars for every test.

    Mirrors :func:`backend.tests.test_ui_topology_table._bff_env` plus
    the broadcast Redis URL the SSE generator's client construction
    reads. Cache + global-state resets run on setup and teardown so a
    failing test cannot leak ``_TEMPLATES`` / session-engine /
    broadcast-client state into the next case.
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
    reset_broadcast_blocking_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _empty_backlog_prelude_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default the UI bridge's prelude ``XREVRANGE`` to "no entries".

    G0.16-T3 (#1305) added the backlog prelude shared between
    ``/api/v1/feed`` and ``/ui/broadcast/stream``. The pre-existing
    UI tests under :class:`TestBroadcastStreamGenerator` pass
    ``cursor="$"`` and only mock ``xread``; without this fixture
    they would hit a real ``redis.asyncio.Redis.xrevrange`` call
    against the test ``BROADCAST_REDIS_URL`` (``broadcast.test``,
    unresolvable in CI / sandbox) and fail with a connection
    error. Patching at the class level mirrors the same fixture in
    :mod:`backend.tests.test_api_v1_feed`.
    """
    import redis.asyncio as redis

    async def _empty(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(redis.Redis, "xrevrange", _empty, raising=True)
    yield


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the broadcast UI tests.

    Mirrors the production wiring + the chassis/topology suites:
    StaticFiles at ``/ui/static``, BFF auth router + UI surface router
    (which now includes the broadcast routes ahead of the stubs),
    ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next. Audit /
    RequestContext middlewares are skipped -- the feed page is read-only
    and the SSE stream's audit plumbing is the API route's concern.
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


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row directly and return its UUID.

    Bypasses the BFF callback round-trip; the route only needs the
    session row to be loadable + decryptable, which
    :func:`create_session` provides synchronously. Mirrors the topology
    suite's helper.
    """
    from meho_backplane.db.engine import get_sessionmaker

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


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_feed_page_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/broadcast`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/broadcast")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_stream_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/broadcast/stream`` without a session 302s to login.

    The bridge lives under ``/ui/`` precisely so the existing
    ``UISessionMiddleware`` gates it; an unauthenticated EventSource
    connect is bounced before it can reach the generator.
    """
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/broadcast/stream")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Full-page render -- 200 + chrome + SSE wiring + empty state + cap
# ---------------------------------------------------------------------------


def test_feed_page_renders_chrome_and_active_sidebar() -> None:
    """The page extends base.html and marks the Broadcast sidebar link active."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Broadcast" in body
    # Sidebar active-state on the Broadcast link.
    assert 'aria-current="page"' in body
    assert "menu-active" in body
    # CSRF cookie set by the route (mirrors dashboard + topology).
    assert CSRF_COOKIE_NAME in response.cookies


def test_feed_page_wires_sse_extension_to_ui_bridge() -> None:
    """The page subscribes via the HTMX sse extension to the UI bridge.

    Critically NOT ``/api/v1/feed`` directly -- the browser EventSource
    cannot send the Bearer header that route requires, so the feed
    subscribes to the session-gated ``/ui/broadcast/stream`` bridge.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    assert 'hx-ext="sse"' in body
    assert 'sse-connect="/ui/broadcast/stream"' in body
    assert 'sse-swap="broadcast"' in body
    # The surface JS that holds the Alpine controller is loaded.
    assert "/ui/static/src/app/broadcast-feed.js" in body
    # The SSE extension itself is loaded by the chassis base layout.
    assert "/ui/static/src/vendor/sse.min.js" in body


def test_feed_page_loads_controller_before_alpine() -> None:
    """The controller script tag precedes the Alpine bundle (#1692).

    Both tags are ``defer``red, and deferred scripts execute in
    document order -- the only ordering that lets the controller's
    ``alpine:init`` listener register before the Alpine CDN bundle
    auto-starts (it fires ``alpine:init`` from a microtask at the end
    of its own script task, before any later deferred script runs). A
    regression here makes Alpine process ``x-data="broadcastFeed(...)"``
    with the component unregistered: both broadcast tabs render dead
    and every SSE frame throws in the console.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    controller_pos = body.index("/ui/static/src/app/broadcast-feed.js")
    alpine_pos = body.index("/ui/static/src/vendor/alpine.min.js")
    assert controller_pos < alpine_pos


def test_feed_page_renders_empty_state() -> None:
    """The empty-state copy renders (shown until the first event lands)."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    assert "No activity matching your filters." in response.text


def test_feed_page_embeds_row_cap_and_badge_palette() -> None:
    """The 1000-row cap + the op_class badge colour table reach the page.

    The cap drives the Alpine trim (work item #9); the badge JSON is the
    server-authored colour policy the row builder reads (work item #2).
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    assert str(IN_DOM_ROW_CAP) in body
    assert IN_DOM_ROW_CAP == 1000
    # The colour table is serialised into the page for the row builder.
    for op_class, badge_class in OP_CLASS_BADGE_CLASSES.items():
        assert op_class in body
        assert badge_class in body


def test_badge_and_filter_tables_cover_the_full_op_class_enum() -> None:
    """Both UI lookup tables span exactly ``OP_CLASS_ENUM`` (#2731).

    Before #2731 the two tables omitted ``approval`` /
    ``credential_write`` / ``checks``: nothing broke — badges fell back
    to ``badge-ghost`` and the classes stayed reachable by hand-typed
    query param — but the filter dropdown could not name them. Set
    equality (not superset) so a class removed from the enum cannot
    linger as a dead dropdown option or badge entry either.
    """
    assert set(OP_CLASS_BADGE_CLASSES) == set(OP_CLASS_ENUM), (
        f"OP_CLASS_BADGE_CLASSES drifted from OP_CLASS_ENUM: "
        f"missing={sorted(set(OP_CLASS_ENUM) - set(OP_CLASS_BADGE_CLASSES))}, "
        f"extra={sorted(set(OP_CLASS_BADGE_CLASSES) - set(OP_CLASS_ENUM))}"
    )
    assert set(OP_CLASS_FILTER_OPTIONS) == set(OP_CLASS_ENUM), (
        f"OP_CLASS_FILTER_OPTIONS drifted from OP_CLASS_ENUM: "
        f"missing={sorted(set(OP_CLASS_ENUM) - set(OP_CLASS_FILTER_OPTIONS))}, "
        f"extra={sorted(set(OP_CLASS_FILTER_OPTIONS) - set(OP_CLASS_ENUM))}"
    )


def test_feed_page_carries_aggregate_only_placeholder_logic() -> None:
    """The aggregate-only placeholder text is wired in the surface JS.

    Credential reads + audit queries broadcast aggregate-only (decision
    #3); the row builder renders ``(aggregate-only)`` when the redacted
    payload carries no ``params`` key.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.get("/ui/broadcast")
        js = client.get("/ui/static/src/app/broadcast-feed.js")
    assert js.status_code == 200, js.text
    assert "(aggregate-only)" in js.text
    assert "payloadSummary" in js.text


def test_broadcast_stub_is_replaced_by_real_route() -> None:
    """The chassis ``broadcast`` stub no longer shadows the real view.

    The stub rendered a "Coming soon" panel; the real route renders the
    live feed. Asserting the placeholder copy is gone proves the stub
    was removed from the enumeration and the real route wins.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    assert "Coming soon" not in body
    assert "Operator surface placeholder." not in body


# ---------------------------------------------------------------------------
# SSE bridge -- tenant scoping, resume checkpoint, filters (#3758)
# ---------------------------------------------------------------------------


def _capture_bridge(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace the shared generator with a finite one; record its inputs."""
    calls: list[dict[str, object]] = []

    async def _finite(operator: object, **kwargs: object) -> AsyncIterator[str]:
        calls.append({"operator": operator, **kwargs})
        yield ": heartbeat\n\n"

    monkeypatch.setattr(stream_module, "announcement_feed_frames", _finite)
    return calls


def test_stream_reads_the_session_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant-B session streams tenant B; there is no tenant parameter."""
    calls = _capture_bridge(monkeypatch)
    session_id = _seed_session_sync(tenant_id=_TENANT_B)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/stream", params={"tenant_id": str(_TENANT_A)})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert calls[0]["operator"].tenant_id == _TENANT_B  # type: ignore[attr-defined]
    assert calls[0]["surface"] == "ui"


def test_stream_resumes_from_a_checkpoint_and_passes_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_bridge(monkeypatch)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    checkpoint = encode_watch_checkpoint(_TENANT_A, 12)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/stream",
            params={"principal": "agent-a", "target": "cluster-x"},
            headers={"Last-Event-Id": checkpoint},
        )
    assert response.status_code == 200
    assert calls[0]["start"] == FeedStart(after=12)
    assert calls[0]["flt"] == AnnouncementFilter(principal="agent-a", target="cluster-x")


def test_stream_stale_last_event_id_starts_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old Valkey id from before #3758 does not close the EventSource."""
    calls = _capture_bridge(monkeypatch)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/stream", headers={"Last-Event-Id": "1715600000000-0"})
    assert response.status_code == 200
    assert calls[0]["start"] == FeedStart()


def test_stream_invalid_since_returns_400() -> None:
    """A malformed ``since`` is rejected at the route boundary."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/stream", params={"since": "not-a-time"})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid_cursor")
