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
* **SSE generator** (``TestBroadcastStreamGenerator``) -- drives the
  UI stream's ``_ui_feed_generator`` directly with a mocked broadcast
  client, mirroring :mod:`backend.tests.test_api_v1_feed`'s generator
  suite. This is where the tenant-scoping (cross-tenant isolation) and
  replay-cursor behaviour are asserted, because the SSE body never
  reaches the TestClient's buffered-response model.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
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
from meho_backplane.ui.routes.broadcast.feed import (
    IN_DOM_ROW_CAP,
    OP_CLASS_BADGE_CLASSES,
)
from meho_backplane.ui.routes.broadcast.stream import _ui_feed_generator
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


def _make_event(
    *,
    tenant_id: UUID = _TENANT_A,
    op_class: str = "read",
    principal_sub: str = "op-test",
    target_name: str | None = "rdc-vcenter",
    op_id: str = "vsphere.vm.list",
) -> BroadcastEvent:
    """Build a redacted-shape :class:`BroadcastEvent` for the stream tests."""
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


def _xread_returning(events: list[BroadcastEvent]) -> AsyncMock:
    """AsyncMock returning *events* once, then idling forever.

    Mirrors :func:`backend.tests.test_api_v1_feed._xread_returning`: the
    first ``xread`` returns the seeded entries; subsequent calls sleep a
    tick and return ``None`` (the BLOCK-timeout shape) so the generator
    has a yield point at which ``aclose()`` can land its
    ``CancelledError``.
    """
    items = [
        (f"{1715600000000 + i}-0", {"event": event.model_dump_json()})
        for i, event in enumerate(events)
    ]
    call_count = {"n": 0}

    async def _xread_side_effect(*_args: object, **_kwargs: object) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("meho:feed:<irrelevant>", items)]
        await asyncio.sleep(0.01)
        return None

    return AsyncMock(side_effect=_xread_side_effect)


async def _collect_n_frames(
    gen: AsyncIterator[str],
    *,
    n: int,
    timeout: float = 1.0,
) -> list[str]:
    """Read up to *n* SSE frames from *gen* then ``aclose()`` it cleanly.

    Mirrors :func:`backend.tests.test_api_v1_feed._collect_n_frames`.
    The ``TimeoutError`` from the wrapper is the termination mechanism
    when *n* is unreachable (e.g. cursor-pass-through with ``n=0``).
    """
    frames: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            async for frame in gen:
                frames.append(frame)
                if len(frames) >= n:
                    break
    except TimeoutError:
        pass
    finally:
        await gen.aclose()
    return frames


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
# SSE generator -- tenant scoping (cross-tenant isolation) + replay cursor
# ---------------------------------------------------------------------------


class TestBroadcastStreamGenerator:
    """Drives :func:`_ui_feed_generator` directly with a mocked client."""

    async def test_basic_event_yield_uses_session_tenant_stream(self) -> None:
        """One event → one SSE frame; the stream key is the session tenant."""
        event = _make_event(tenant_id=_TENANT_A)
        broadcast_client = get_broadcast_client()
        mock = _xread_returning([event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _ui_feed_generator(
                tenant_id=_TENANT_A,
                operator_sub="op-a",
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        assert mock.await_count >= 1
        # The stream key is derived from the session tenant -- the same
        # key the publisher writes, so the UI tails the right stream.
        stream_key_arg = mock.await_args_list[0].args[0]
        assert stream_key_arg == {f"meho:feed:{_TENANT_A}": "$"}

        assert len(frames) == 1
        frame = frames[0]
        # Byte-compatible frame shape with /api/v1/feed (reused helpers).
        assert frame.startswith("event: broadcast\n")
        assert "id: 1715600000000-0" in frame
        data_line = next(line for line in frame.split("\n") if line.startswith("data: "))
        decoded = json.loads(data_line[len("data: ") :])
        assert decoded["tenant_id"] == str(_TENANT_A)

    async def test_tenant_b_session_reads_only_tenant_b_stream(self) -> None:
        """Cross-tenant isolation: a tenant-B session tails the B stream.

        The generator takes the tenant id from the session, never a
        query parameter, so a tenant-B operator's stream key is
        ``meho:feed:{B}`` -- it can never read tenant-A's stream.
        """
        broadcast_client = get_broadcast_client()
        # The mock returns no entries (idle); we only assert the key.
        mock = AsyncMock(return_value=None)
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _ui_feed_generator(
                tenant_id=_TENANT_B,
                operator_sub="op-b",
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            await _collect_n_frames(gen, n=0)

        assert mock.await_count >= 1
        stream_key_arg = mock.await_args_list[0].args[0]
        assert stream_key_arg == {f"meho:feed:{_TENANT_B}": "$"}
        assert f"meho:feed:{_TENANT_A}" not in stream_key_arg

    async def test_replay_cursor_passes_through_to_xread(self) -> None:
        """An explicit cursor (replay) is forwarded verbatim to xread.

        Reconnect via ``Last-Event-Id`` resolves to this cursor; the
        feed cursor resolver + validator are reused, so a reconnect that
        started on ``/api/v1/feed`` replays identically through the
        bridge.
        """
        broadcast_client = get_broadcast_client()
        mock = AsyncMock(return_value=None)
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _ui_feed_generator(
                tenant_id=_TENANT_A,
                operator_sub="op-a",
                cursor="1715600000000-0",
                op_class=None,
                principal=None,
                target=None,
            )
            await _collect_n_frames(gen, n=0)

        assert mock.await_count >= 1
        cursor_arg = mock.await_args_list[0].args[0]
        assert cursor_arg == {f"meho:feed:{_TENANT_A}": "1715600000000-0"}

    async def test_op_class_filter_drops_non_matching_events(self) -> None:
        """A non-None op_class filter narrows the yielded frames."""
        read_event = _make_event(op_class="read", op_id="vsphere.vm.list")
        write_event = _make_event(op_class="write", op_id="vsphere.vm.create")
        broadcast_client = get_broadcast_client()
        mock = _xread_returning([read_event, write_event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _ui_feed_generator(
                tenant_id=_TENANT_A,
                operator_sub="op-a",
                cursor="$",
                op_class="read",
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        assert len(frames) == 1
        decoded = json.loads(
            next(line for line in frames[0].split("\n") if line.startswith("data: "))[
                len("data: ") :
            ]
        )
        assert decoded["op_class"] == "read"


# ---------------------------------------------------------------------------
# Stream cursor validation at the HTTP edge
# ---------------------------------------------------------------------------


def test_stream_invalid_last_event_id_returns_400() -> None:
    """A malformed ``Last-Event-Id`` is rejected at the route boundary.

    Returning 400 before streaming flips the EventSource state machine
    to CLOSED rather than letting it hot-loop with the same bad cursor
    -- the same guard ``/api/v1/feed`` applies, reused here.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/stream",
            headers={"Last-Event-Id": "not-a-valkey-id"},
        )
    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid_cursor")
